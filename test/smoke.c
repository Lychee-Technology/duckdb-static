// Links libduckdb_bundle.a the way a consumer does and runs a query against each
// bundled extension, comparing results rather than only checking for success.
// The registered builds also check core SQL, JSON and Parquet behavior in more
// depth and reopen a database file. test/smoke.sh builds it three ways:
//
//   -DSMOKE_REGISTER   calls duckdb_register_static_extensions() before opening
//   (neither define)   is linked with -Wl,-u,duckdb_register_static_extensions
//                      and relies on the loader's before-main hook
//   -DSMOKE_UNLINKED   neither: expects the extensions to be missing, which
//                      shows that the registration, not a download, provides them
//
// Usage: smoke <expected version> <data dir> <host:port of test/smoke.sh's server>
// The server serves <data dir>, so a file written there can be read back over
// HTTP and, under bucket/, over S3. <data dir> must start empty: the database
// file goes there too, and the checks expect it not to exist yet.
//
// Prints one line per check and ends with "<n> checks passed". It fails if any
// check failed or if none ran.
#include "duckdb.h"

#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef SMOKE_REGISTER
int32_t duckdb_register_static_extensions(void);
#endif

// DuckDB's error for INSTALL, and for LOAD of an extension that isn't registered,
// in a build without loadable-extension support.
#define NO_LOADABLE "this build does not link the loadable_extensions library"

static duckdb_database db;
static duckdb_connection con;
static int passes, failures;

static void pass(const char *sql, const char *outcome) {
	printf("ok   %s -> %s\n", sql, outcome);
	passes++;
}

static void fail(const char *sql, const char *message) {
	printf("FAIL %s\n     %s\n", sql, message);
	failures++;
}

// Opens the database file at path, or an in-memory database when path is NULL,
// and points the checks at it.
static bool open_database(const char *path) {
	char *error = NULL;
	char what[4096];
	snprintf(what, sizeof(what), "open %s", path ? path : "an in-memory database");
	if (duckdb_open_ext(path, &db, NULL, &error) != DuckDBSuccess) {
		fail(what, error ? error : "unknown error");
		duckdb_free(error);
		return false;
	}
	if (duckdb_connect(db, &con) != DuckDBSuccess) {
		fail(what, "could not connect");
		duckdb_close(&db);
		return false;
	}
	return true;
}

static void close_database(void) {
	duckdb_disconnect(&con);
	duckdb_close(&db);
}

static char *format_sql(const char *format, va_list args) {
	static char sql[4096];
	vsnprintf(sql, sizeof(sql), format, args);
	return sql;
}

// Checks that the query succeeds and that the first value of its result is
// expected, or is NULL when expected is NULL.
__attribute__((format(printf, 2, 3))) static void expect(const char *expected, const char *format, ...) {
	va_list args;
	va_start(args, format);
	const char *sql = format_sql(format, args);
	va_end(args);

	duckdb_result result;
	if (duckdb_query(con, sql, &result) != DuckDBSuccess) {
		fail(sql, duckdb_result_error(&result));
	} else if (duckdb_row_count(&result) == 0 || duckdb_column_count(&result) == 0) {
		fail(sql, "no result");
	} else {
		char *got = duckdb_value_is_null(&result, 0, 0) ? NULL : duckdb_value_varchar(&result, 0, 0);
		if (expected ? got && strcmp(got, expected) == 0 : !got) {
			pass(sql, got ? got : "NULL");
		} else {
			// Strings are quoted, so a NULL can't be mistaken for the string 'NULL'.
			char want[1024] = "NULL", have[1024] = "NULL", message[2100];
			if (expected) {
				snprintf(want, sizeof(want), "'%s'", expected);
			}
			if (got) {
				snprintf(have, sizeof(have), "'%s'", got);
			}
			snprintf(message, sizeof(message), "expected %s, got %s", want, have);
			fail(sql, message);
		}
		duckdb_free(got);
	}
	duckdb_destroy_result(&result);
}

// Checks that the query fails with an error containing fragment.
__attribute__((format(printf, 2, 3))) static void expect_error(const char *fragment, const char *format, ...) {
	va_list args;
	va_start(args, format);
	const char *sql = format_sql(format, args);
	va_end(args);

	duckdb_result result;
	if (duckdb_query(con, sql, &result) == DuckDBSuccess) {
		fail(sql, "succeeded, expected an error");
	} else if (!strstr(duckdb_result_error(&result), fragment)) {
		char message[2048];
		snprintf(message, sizeof(message), "expected an error containing '%s', got: %s", fragment,
		         duckdb_result_error(&result));
		fail(sql, message);
	} else {
		char outcome[1024];
		snprintf(outcome, sizeof(outcome), "error containing '%s'", fragment);
		pass(sql, outcome);
	}
	duckdb_destroy_result(&result);
}

// Checks that the query fails with an error of the given duckdb_error_type. The
// type is part of the C API, so it doesn't change when DuckDB rewords a message.
// The unlinked build doesn't use it.
#define expect_error_type(type, ...) check_error_type(type, #type, __VA_ARGS__)
__attribute__((format(printf, 3, 4), unused)) static void check_error_type(duckdb_error_type type,
                                                                           const char *type_name,
                                                                           const char *format, ...) {
	va_list args;
	va_start(args, format);
	const char *sql = format_sql(format, args);
	va_end(args);

	duckdb_result result;
	if (duckdb_query(con, sql, &result) == DuckDBSuccess) {
		fail(sql, "succeeded, expected an error");
	} else if (duckdb_result_error_type(&result) != type) {
		char message[2048];
		snprintf(message, sizeof(message), "expected %s (%d), got error type %d: %s", type_name, (int)type,
		         (int)duckdb_result_error_type(&result), duckdb_result_error(&result));
		fail(sql, message);
	} else {
		pass(sql, type_name);
	}
	duckdb_destroy_result(&result);
}

#ifndef SMOKE_UNLINKED
// Core SQL on two small tables. Multi-row results are folded into one value with
// list(... ORDER BY ...), whose text form spells out NULLs.
static void check_core(void) {
	expect("3", "CREATE TABLE ponds (id INTEGER PRIMARY KEY, pond VARCHAR); "
	            "INSERT INTO ponds VALUES (1, 'Mill'), (2, 'Lily'), (3, 'Reed')");
	expect("4", "CREATE TABLE ducks (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, pond_id INTEGER, "
	            "weight DECIMAL(4, 2)); "
	            "INSERT INTO ducks VALUES (1, 'Wilbur', 1, 1.25), (2, 'Daisy', 1, NULL), (3, 'Huey', 2, 0.75), "
	            "(4, 'Dewey', NULL, 1.10)");
	expect("id INTEGER, name VARCHAR, pond_id INTEGER, weight DECIMAL(4,2)",
	       "SELECT string_agg(column_name || ' ' || data_type, ', ' ORDER BY ordinal_position) "
	       "FROM information_schema.columns WHERE table_name = 'ducks'");

	// joins: a NULL key matches nothing
	expect("[(Wilbur, Mill), (Daisy, Mill), (Huey, Lily)]",
	       "SELECT list((d.name, p.pond) ORDER BY d.id)::VARCHAR FROM ducks d JOIN ponds p ON d.pond_id = p.id");
	expect("[(Wilbur, Mill), (Daisy, Mill), (Huey, Lily), (Dewey, NULL), (NULL, Reed)]",
	       "SELECT list((d.name, p.pond) ORDER BY d.id NULLS LAST)::VARCHAR "
	       "FROM ducks d FULL JOIN ponds p ON d.pond_id = p.id");
	expect("[Reed]", "SELECT list(pond)::VARCHAR FROM ponds p "
	                 "WHERE NOT EXISTS (SELECT 1 FROM ducks d WHERE d.pond_id = p.id)");

	// aggregation: NULL keys form one group, and aggregates skip NULL values
	expect("[(1, 2, 1.25), (2, 1, 0.75), (NULL, 1, 1.10)]",
	       "SELECT list((pond_id, n, total) ORDER BY pond_id NULLS LAST)::VARCHAR "
	       "FROM (SELECT pond_id, count(*) AS n, sum(weight) AS total FROM ducks GROUP BY pond_id)");
	expect("[1]", "SELECT list(pond_id)::VARCHAR FROM (SELECT pond_id FROM ducks GROUP BY pond_id HAVING count(*) > 1)");
	expect("(4, 3, 2, 0.75, 1.25)", "SELECT (count(*), count(weight), count(DISTINCT pond_id), min(weight), "
	                                "max(weight))::VARCHAR FROM ducks");

	// ordering, of an aggregate's input and of a result with LIMIT and OFFSET
	expect("[Daisy, Wilbur, Dewey, Huey]", "SELECT list(name ORDER BY weight DESC NULLS FIRST)::VARCHAR FROM ducks");
	expect("[Huey, Dewey, Wilbur, Daisy]", "SELECT list(name ORDER BY weight NULLS LAST)::VARCHAR FROM ducks");
	expect("Daisy", "SELECT name FROM ducks ORDER BY pond_id NULLS FIRST, name DESC LIMIT 1 OFFSET 2");

	// NULLs
	expect("[NULL, true, false, true, NULL]",
	       "SELECT [NULL = NULL, NULL IS NOT DISTINCT FROM NULL, NULL AND false, NULL OR true, NULL AND true]::VARCHAR");
	expect("[7, NULL, NULL]", "SELECT [coalesce(NULL, 7), nullif(3, 3), 1 + NULL]::VARCHAR");
	expect(NULL, "SELECT sum(weight) FROM ducks WHERE weight IS NULL");
	expect("3", "SELECT count(*) FROM (SELECT DISTINCT pond_id FROM ducks)");
	// NOT IN against a list that holds a NULL is never true.
	expect("0", "SELECT count(*) FROM ponds WHERE id NOT IN (SELECT pond_id FROM ducks)");

	// mutations
	expect("5", "INSERT INTO ducks VALUES (5, 'Louie', 2, 0.80) RETURNING id");
	expect("1", "UPDATE ducks SET weight = 1.30 WHERE weight IS NULL");
	expect("1", "DELETE FROM ducks WHERE pond_id IS NULL");
	expect("1", "INSERT INTO ducks VALUES (3, 'Huey', 3, 0.90) "
	            "ON CONFLICT (id) DO UPDATE SET pond_id = excluded.pond_id, weight = excluded.weight");
	expect("4", "BEGIN; DELETE FROM ducks; ROLLBACK; SELECT count(*) FROM ducks");
	// A statement that fails changes nothing, including rows before the one that failed.
	expect_error_type(DUCKDB_ERROR_CONSTRAINT, "INSERT INTO ducks VALUES (6, 'Scrooge', 1, 2.00), (1, 'Wilbur', 1, 1.25)");
	expect_error_type(DUCKDB_ERROR_CONSTRAINT, "INSERT INTO ducks (id, name) VALUES (7, NULL)");
	expect("[(1, Wilbur, 1, 1.25), (2, Daisy, 1, 1.30), (3, Huey, 3, 0.90), (5, Louie, 2, 0.80)]",
	       "SELECT list((id, name, pond_id, weight) ORDER BY id)::VARCHAR FROM ducks");
	expect("[(1, [new]), (2, [new]), (3, [new]), (5, [new])]",
	       "ALTER TABLE ducks ADD COLUMN tags VARCHAR[] DEFAULT ['new']; "
	       "SELECT list((id, tags) ORDER BY id)::VARCHAR FROM ducks");

	// errors, by type, which DuckDB keeps stable where it rewords messages
	expect_error_type(DUCKDB_ERROR_PARSER, "SELEC 1");
	expect_error_type(DUCKDB_ERROR_CATALOG, "SELECT * FROM no_such_table");
	expect_error_type(DUCKDB_ERROR_BINDER, "SELECT no_such_column FROM ducks");
	expect_error_type(DUCKDB_ERROR_CONVERSION, "SELECT 'duck'::INTEGER");
	expect_error_type(DUCKDB_ERROR_OUT_OF_RANGE, "SELECT 2147483647::INTEGER + 1");
}

// Nested JSON values, and a JSON file written and read back.
static void check_json(const char *dir) {
	expect("[\"a\",\"b\"]", "SELECT json_extract('{\"duck\": {\"tags\": [\"a\", \"b\"]}}', '$.duck.tags')::VARCHAR");
	expect("Dewey", "SELECT '{\"ducks\": [{\"name\": \"Huey\"}, {\"name\": \"Dewey\"}]}'::JSON->'$.ducks[1]'->>'name'");
	// A JSON null is a value of JSON type 'NULL' that, like a missing key, reads as SQL NULL.
	expect("(NULL, 'NULL', NULL, true, false)",
	       "SELECT ('{\"a\": null}'::JSON->>'$.a', json_type('{\"a\": null}', '$.a'), '{\"a\": null}'::JSON->>'$.b', "
	       "json_exists('{\"a\": null}', '$.a'), json_exists('{\"a\": null}', '$.b'))::VARCHAR");
	expect("{'name': Wilbur, 'tags': [x, NULL], 'pond': {'id': 1}}",
	       "SELECT from_json('{\"name\": \"Wilbur\", \"tags\": [\"x\", null], \"pond\": {\"id\": 1}}', "
	       "'{\"name\": \"VARCHAR\", \"tags\": [\"VARCHAR\"], \"pond\": {\"id\": \"INTEGER\"}}')::VARCHAR");
	expect("{\"name\":\"Wilbur\",\"tags\":[\"x\",null],\"pond\":{\"id\":1,\"note\":null}}",
	       "SELECT to_json({'name': 'Wilbur', 'tags': ['x', NULL], 'pond': {'id': 1, 'note': NULL}})::VARCHAR");
	expect_error_type(DUCKDB_ERROR_CONVERSION, "SELECT '{\"a\": }'::JSON");

	expect("3", "COPY (SELECT i AS id, CASE WHEN i = 2 THEN NULL ELSE {'name': 'duck ' || i, "
	            "'tags': ['t' || i, CASE WHEN i = 1 THEN NULL ELSE 'x' END]} END AS info FROM range(3) t(i)) "
	            "TO '%s/ducks.json'",
	       dir);
	expect("STRUCT(\"name\" VARCHAR, tags VARCHAR[])", "SELECT typeof(info) FROM read_json('%s/ducks.json') LIMIT 1",
	       dir);
	expect("[(0, {'name': duck 0, 'tags': [t0, x]}), (1, {'name': duck 1, 'tags': [t1, NULL]}), (2, NULL)]",
	       "SELECT list((id, info) ORDER BY id)::VARCHAR FROM read_json('%s/ducks.json')", dir);
}

// A local Parquet file with NULLs, lists and structs, written in 10 row groups
// so that filters can skip some of them.
static void check_parquet(const char *dir) {
	expect("100000", "CREATE TABLE nested_src AS SELECT i AS id, "
	                 "CASE WHEN i %% 10 = 0 THEN NULL ELSE 'duck ' || i END AS name, "
	                 "(i %% 1000 / 4)::DECIMAL(9, 2) AS price, "
	                 "DATE '2024-01-01' + (i %% 366)::INTEGER AS day, "
	                 "CASE WHEN i %% 7 = 0 THEN NULL WHEN i %% 7 = 1 THEN [i, NULL] ELSE [i, i + 1] END AS pair, "
	                 "{'k': i %% 3, 'v': CASE WHEN i %% 4 = 0 THEN NULL ELSE 'v' || i END} AS info "
	                 "FROM range(100000) t(i); "
	                 "COPY nested_src TO '%s/nested.parquet' (FORMAT parquet, ROW_GROUP_SIZE 10000)",
	       dir);
	expect("10", "SELECT count(DISTINCT row_group_id) FROM parquet_metadata('%s/nested.parquet')", dir);
	expect("id BIGINT, name VARCHAR, price DECIMAL(9,2), day DATE, pair BIGINT[], info STRUCT(k BIGINT, v VARCHAR)",
	       "CREATE VIEW nested AS FROM '%s/nested.parquet'; "
	       "SELECT string_agg(column_name || ' ' || data_type, ', ' ORDER BY ordinal_position) "
	       "FROM information_schema.columns WHERE table_name = 'nested'",
	       dir);

	// projection, including a single struct field
	expect("4999950000", "SELECT sum(id) FROM nested");
	expect("[NULL, v1, v2, v3, NULL]", "SELECT list(info.v ORDER BY id)::VARCHAR FROM nested WHERE id < 5");

	// predicates, on a column, on NULLs and on a struct field
	expect("(10000, 25000, 34999)", "SELECT (count(*), min(id), max(id))::VARCHAR FROM nested "
	                                "WHERE id BETWEEN 25000 AND 34999");
	expect("10000", "SELECT count(*) FROM nested WHERE name IS NULL");
	expect("8333", "SELECT count(*) FROM nested WHERE info.k = 2 AND info.v IS NULL");

	// NULLs at the top level, in a list and in a struct, and nested values
	expect("(100000, 90000, 85714, 75000)",
	       "SELECT (count(*), count(name), count(pair), count(info.v))::VARCHAR FROM nested");
	expect("14286", "SELECT count(*) FROM (SELECT unnest(pair) AS u FROM nested) WHERE u IS NULL");
	expect("[(0, NULL, 0.00, 2024-01-01, NULL, {'k': 0, 'v': NULL}), "
	       "(1, duck 1, 0.25, 2024-01-02, [1, NULL], {'k': 1, 'v': v1}), "
	       "(99999, duck 99999, 249.75, 2024-03-22, [99999, 100000], {'k': 0, 'v': v99999})]",
	       "SELECT list((id, name, price, day, pair, info) ORDER BY id)::VARCHAR FROM nested "
	       "WHERE id IN (0, 1, 99999)");
	// Every row comes back as written.
	expect("0", "SELECT count(*) FROM (FROM nested_src EXCEPT ALL FROM nested)");
	expect_error_type(DUCKDB_ERROR_IO, "SELECT * FROM '%s/missing.parquet'", dir);
}

// Writes a database file, then reopens it to check that the rows, including
// NULLs and nested values, and later changes to them come back from disk.
static void check_reopen(const char *path) {
	if (!open_database(path)) {
		return;
	}
	// The file is new: nothing is left over from an earlier run.
	expect("0", "SELECT count(*) FROM duckdb_tables()");
	expect("3", "CREATE TABLE notes (id INTEGER PRIMARY KEY, body VARCHAR, tags VARCHAR[], "
	            "meta STRUCT(n INTEGER, ok BOOLEAN)); "
	            "INSERT INTO notes VALUES (1, 'first', ['a', NULL], {'n': 1, 'ok': true}), (2, NULL, NULL, NULL), "
	            "(3, 'third', [], {'n': NULL, 'ok': false})");
	// More rows than fit in one row group
	expect("300000", "CREATE TABLE big AS SELECT i, CASE WHEN i %% 5 = 0 THEN NULL ELSE 'v' || i END AS s "
	                 "FROM range(300000) t(i); SELECT count(*) FROM big");
	close_database();

	if (!open_database(path)) {
		return;
	}
	expect("[(1, first, [a, NULL], {'n': 1, 'ok': true}), (2, NULL, NULL, NULL), "
	       "(3, third, [], {'n': NULL, 'ok': false})]",
	       "SELECT list((id, body, tags, meta) ORDER BY id)::VARCHAR FROM notes");
	expect("(300000, 44999850000, 240000)", "SELECT (count(*), sum(i), count(s))::VARCHAR FROM big");
	expect("200000", "UPDATE notes SET body = 'second' WHERE id = 2; DELETE FROM notes WHERE id = 3; "
	                 "DELETE FROM big WHERE i >= 100000");
	close_database();

	if (!open_database(path)) {
		return;
	}
	expect("[(1, first), (2, second)]", "SELECT list((id, body) ORDER BY id)::VARCHAR FROM notes");
	expect("(100000, 4999950000, 80000)", "SELECT (count(*), sum(i), count(s))::VARCHAR FROM big");
	close_database();
}
#endif

int main(int argc, char **argv) {
	if (argc != 4) {
		fprintf(stderr, "Usage: %s <expected version> <data dir> <host:port>\n", argv[0]);
		return 2;
	}
	const char *version = argv[1], *dir = argv[2], *endpoint = argv[3];
	// Line-buffered, so a crash still shows the checks before it
	setvbuf(stdout, NULL, _IOLBF, 0);

#ifdef SMOKE_REGISTER
	if (duckdb_register_static_extensions() != 0) {
		fprintf(stderr, "duckdb_register_static_extensions failed\n");
		return 1;
	}
#endif
	if (!open_database(NULL)) {
		return 1;
	}

	expect(version, "SELECT library_version FROM pragma_version()");
	expect("42", "SELECT 42");

#ifdef SMOKE_UNLINKED
	(void)endpoint;
	expect("0", "SELECT count(*) FROM duckdb_extensions() WHERE loaded");
	expect_error("core_functions", "SELECT to_minutes(5)");
	// Nothing registered parquet, and the bundle can't load it from anywhere else.
	expect_error(NO_LOADABLE, "LOAD parquet");
	expect_error("parquet", "COPY (SELECT 1 AS i) TO '%s/unlinked.parquet' (FORMAT parquet)", dir);
	expect_error("json", "SELECT json_extract_string('{\"a\": 1}', '$.a')");
#else
	expect("core_functions,httpfs,icu,json,parquet",
	       "SELECT string_agg(extension_name, ',' ORDER BY extension_name) FROM duckdb_extensions() "
	       "WHERE loaded AND install_mode = 'STATICALLY_LINKED'");
	// LOAD of a linked extension is a no-op, which is what example/cmd/main.go relies on.
	expect("Wilbur", "LOAD parquet; SELECT 'Wilbur'");

	// core_functions
	expect("00:05:00", "SELECT to_minutes(5)");
	expect("6", "SELECT list_sum([1, 2, 3])");

	// json
	expect("Wilbur", "SELECT json_extract_string('{\"duck\": {\"name\": \"Wilbur\"}}', '$.duck.name')");
	expect("3", "SELECT json_array_length('[1, 2, 3]'::JSON)");

	// parquet, written under bucket/ so the S3 check below can read the same file
	expect("10000", "COPY (SELECT i, 'row ' || i AS s FROM range(10000) t(i)) TO '%s/bucket/smoke.parquet' "
	                "(FORMAT parquet)",
	       dir);
	expect("49995000", "SELECT sum(i) FROM read_parquet('%s/bucket/smoke.parquet')", dir);
	expect("row 9999", "SELECT max_by(s, i) FROM '%s/bucket/smoke.parquet'", dir);

	// icu: a named time zone, and a German collation that sorts ä next to a
	expect("2024-07-01 08:00:00", "SELECT (TIMESTAMPTZ '2024-07-01 12:00:00+00' AT TIME ZONE 'America/New_York')::VARCHAR");
	expect("a,ä,b", "SELECT string_agg(x, ',' ORDER BY x COLLATE de) FROM (VALUES ('b'), ('ä'), ('a')) t(x)");

	// httpfs over plain HTTP, then over S3 with path-style URLs against the same server
	expect("49995000", "SELECT sum(i) FROM read_parquet('http://%s/bucket/smoke.parquet')", endpoint);
	expect("true", "CREATE SECRET smoke (TYPE s3, KEY_ID 'smoke', SECRET 'smoke', REGION 'us-east-1', "
	               "ENDPOINT '%s', URL_STYLE 'path', USE_SSL false)",
	       endpoint);
	expect("49995000", "SELECT sum(i) FROM read_parquet('s3://bucket/smoke.parquet')");

	// An extension that isn't in the bundle can't be installed or loaded: the
	// bundle is built without the loadable-extension support that downloads them.
	expect_error(NO_LOADABLE, "INSTALL tpch");
	expect_error(NO_LOADABLE, "LOAD tpch");

	check_core();
	check_json(dir);
	check_parquet(dir);
#endif
	close_database();

#ifndef SMOKE_UNLINKED
	char path[4096];
	snprintf(path, sizeof(path), "%s/smoke.duckdb", dir);
	check_reopen(path);
#endif

	if (failures) {
		printf("%d of %d checks failed\n", failures, passes + failures);
		return 1;
	}
	if (!passes) {
		printf("no checks ran\n");
		return 1;
	}
	printf("%d checks passed\n", passes);
	return 0;
}
