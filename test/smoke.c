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
// Usage: smoke <expected version> <data dir> <host:port of test/smoke.sh's server> <fixtures dir>
//        smoke <expected version> --remote <base URL> <fixtures dir>
// The server serves <data dir>, so a file written there can be read back over
// HTTP and, under bucket/, over S3, and serves <fixtures dir>, which is
// test/fixtures/public, under public/. <data dir> must start empty: the
// database file goes there too, and the checks expect it not to exist yet.
//
// The registered builds run the queries in the fixtures' manifest.json against
// the server (check_fixtures). With --remote, they run only those, against
// <base URL>, a public HTTPS host serving the same files.
//
// Prints one line per check and ends with "<n> checks passed". It exits 0 if
// every check passed and at least one ran, 3 if every failed check failed in
// the network or with an HTTP error status, which points at the host rather
// than the bundle, 2 on a usage error or a GITHUB_TOKEN it can't use, and 1
// otherwise.
#include "duckdb.h"
// The tarball's other header. Nothing else compiles it, and no shipped header
// declares duckdb_register_static_extensions, so it is declared below.
#include "duckdb_static_extension.h"

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

// The fixture version the fixture checks read.
#define FIXTURES "v1"
// The URLs that get GITHUB_TOKEN in --remote mode. The prefix is fixed rather
// than taken from the base URL, so a mistyped base URL never receives the token.
#define REMOTE_SCOPE "https://raw.githubusercontent.com/Lychee-Technology/duckdb-static/"

static duckdb_database db;
static duckdb_connection con;
// host_failures counts the failures that host_layer() puts in the network or
// on the HTTP server.
static int passes, failures, host_failures;
// The bearer token of the fixtures' secret. No output shows it.
static const char *token;

// Prints text with the token replaced, even where an error quotes the statement
// that holds it.
static void print_redacted(const char *text) {
	size_t length = token ? strlen(token) : 0;
	const char *found;
	while (length && (found = strstr(text, token))) {
		printf("%.*s<redacted>", (int)(found - text), text);
		text = found + length;
	}
	fputs(text, stdout);
}

static void pass(const char *sql, const char *outcome) {
	fputs("ok   ", stdout);
	print_redacted(sql);
	fputs(" -> ", stdout);
	print_redacted(outcome);
	putchar('\n');
	passes++;
}

static void fail(const char *sql, const char *message) {
	fputs("FAIL ", stdout);
	print_redacted(sql);
	fputs("\n     ", stdout);
	print_redacted(message);
	putchar('\n');
	failures++;
}

// Returns the status in an HTTP error's message, "(HTTP 404 Not Found)" or
// "returned HTTP 429", or 0 if there is none.
static int http_status(const char *message) {
	for (const char *at = strstr(message, "HTTP "); at; at = strstr(at + 1, "HTTP ")) {
		int status, end = 0;
		if (sscanf(at, "HTTP %3d%n", &status, &end) == 1 && end == 8) {
			return status;
		}
	}
	return 0;
}

// Names the layer a failed query's error comes from when it is the network or
// the HTTP server, or returns NULL when the error points at the bundle. httpfs
// raises an HTTP error status as DUCKDB_ERROR_HTTP, and a transport failure as
// DUCKDB_ERROR_IO with libcurl's description and "error for HTTP". The message
// only picks the label: the error type decides whether it is the host's.
static const char *host_layer(duckdb_result *result, char *label, size_t size) {
	const char *message = duckdb_result_error(result);
	if (duckdb_result_error_type(result) == DUCKDB_ERROR_HTTP) {
		int status = http_status(message);
		if (status) {
			// smoke.sh's rate-limited: line looks for "HTTP 429, rate-limited".
			snprintf(label, size, "host: HTTP %d%s", status, status == 429 ? ", rate-limited" : "");
		} else {
			// httpfs's error for a host that answers a ranged GET with the whole file
			snprintf(label, size, "host: %s", strstr(message, "range") ? "byte ranges" : "HTTP");
		}
	} else if (duckdb_result_error_type(result) == DUCKDB_ERROR_IO && strstr(message, " error for HTTP ")) {
		const char *layer = strstr(message, "resolve")   ? "DNS"
		                    : strstr(message, "SSL")     ? "TLS"
		                    : strstr(message, "Timeout") ? "timeout"
		                                                 : "connection";
		snprintf(label, size, "network: %s", layer);
	} else {
		return NULL;
	}
	return label;
}

// Fails a check whose query returned an unexpected error, labeled with its
// layer when that is the network or the HTTP server.
static void fail_error(const char *sql, duckdb_result *result, const char *expectation) {
	char label[64], message[2200];
	const char *layer = host_layer(result, label, sizeof(label));
	snprintf(message, sizeof(message), "%s%s%s%s%s", layer ? "[" : "", layer ? layer : "", layer ? "] " : "",
	         expectation, duckdb_result_error(result));
	fail(sql, message);
	if (layer) {
		host_failures++;
	}
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
		fail_error(sql, &result, "");
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
		char expectation[128];
		snprintf(expectation, sizeof(expectation), "expected %s (%d), got error type %d: ", type_name, (int)type,
		         (int)duckdb_result_error_type(&result));
		fail_error(sql, &result, expectation);
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

// Returns the first value of the query's result, to free with duckdb_free. If
// the query fails or returns no value, it fails a check and returns NULL.
__attribute__((format(printf, 1, 2))) static char *value_of(const char *format, ...) {
	va_list args;
	va_start(args, format);
	const char *sql = format_sql(format, args);
	va_end(args);

	duckdb_result result;
	char *value = NULL;
	if (duckdb_query(con, sql, &result) != DuckDBSuccess) {
		fail_error(sql, &result, "");
	} else if (duckdb_row_count(&result) == 0 || duckdb_column_count(&result) == 0 ||
	           duckdb_value_is_null(&result, 0, 0)) {
		fail(sql, "no value");
	} else {
		value = duckdb_value_varchar(&result, 0, 0);
	}
	duckdb_destroy_result(&result);
	return value;
}

// Checks that the query fails with an HTTP error of the given status. A type
// check alone isn't enough, because a rate-limited host's 429 is an HTTP error
// too.
__attribute__((format(printf, 2, 3))) static void expect_http_error(int status, const char *format, ...) {
	va_list args;
	va_start(args, format);
	const char *sql = format_sql(format, args);
	va_end(args);

	duckdb_result result;
	char expectation[64];
	snprintf(expectation, sizeof(expectation), "HTTP %d", status);
	if (duckdb_query(con, sql, &result) == DuckDBSuccess) {
		fail(sql, "succeeded, expected an HTTP error");
	} else if (duckdb_result_error_type(&result) == DUCKDB_ERROR_HTTP &&
	           http_status(duckdb_result_error(&result)) == status) {
		pass(sql, expectation);
	} else {
		strcat(expectation, " expected, got: ");
		fail_error(sql, &result, expectation);
	}
	duckdb_destroy_result(&result);
}

// Returns whether the host failed a check since host_failures was before. The
// fixture checks stop then: the rest would fail the same way and only add
// requests to a host that may be rate-limiting them.
static bool host_failed(int before) {
	static bool said;
	if (host_failures > before && !said) {
		printf("stop the host failed, so the remaining fixture checks are skipped\n");
		said = true;
	}
	return host_failures > before;
}

// The requests httpfs made, from DuckDB's HTTP log
#define HTTP_LOG "duckdb_logs_parsed('HTTP')"
// The byte range of each successful ranged GET in the HTTP log
#define GET_RANGES                                                                                                     \
	"(SELECT request.url AS url, regexp_extract(request.headers['Range'], 'bytes=(\\d+)-(\\d+)', ['first_byte', "      \
	"'last_byte'])::STRUCT(first_byte BIGINT, last_byte BIGINT) AS r FROM " HTTP_LOG " "                               \
	"WHERE request.type = 'GET' AND response.status = 'PartialContent_206')"

static void report_requests(const char *when) {
	char *summary = value_of("SELECT format('{} HTTP requests ({} HEAD, {} GET), {} bytes fetched', count(*), "
	                         "count(*) FILTER (request.type = 'HEAD'), count(*) FILTER (request.type = 'GET'), "
	                         "(SELECT coalesce(sum(r.last_byte - r.first_byte + 1), 0) FROM " GET_RANGES ")) "
	                         "FROM " HTTP_LOG);
	if (summary) {
		printf("info %s %s\n", summary, when);
	}
	duckdb_free(summary);
}

// Runs the queries in the fixtures' manifest.json against <base>/v1 the way a
// consumer reads remote Parquet, sending token to URLs under scope. smoke.sh
// runs this against its local server, and against a public HTTPS host when one
// is configured. Both serve the same files, so a failure on only the public
// host points at the host or the network.
static void check_fixtures(const char *base, const char *fixtures, const char *scope) {
	const int before = host_failures;
	char dir[1024], manifest[1024];
	size_t length = strlen(base);
	if (length && base[length - 1] == '/') {
		length--;
	}
	snprintf(dir, sizeof(dir), "%.*s/" FIXTURES, (int)length, base);
	snprintf(manifest, sizeof(manifest), "%s/" FIXTURES "/manifest.json", fixtures);

	// Nothing may be installed or loaded on demand: the extensions come from
	// the registration. Retries come only from httpfs's settings, never from
	// re-running a query, so a wrong value can't be retried into a pass. On a
	// 429 or 503 httpfs retries up to 5 more times and waits as long as
	// Retry-After asks, up to 20 s. A host that ignores Range fails instead of
	// being read whole. DuckDB fetches a remote file of up to one cache block,
	// 2 MiB by default, in one request, so a smaller block makes the scans of
	// multi_row_group.parquet fetch only the byte ranges they need, as they
	// would in a larger file. The metadata cache saves a HEAD per query.
	expect("0", "SET autoinstall_known_extensions = false; SET autoload_known_extensions = false; "
	            "SET http_retries = 3; SET http_retry_wait_ms = 500; SET http_retry_backoff = 2; "
	            "SET http_timeout = 30; SET auto_fallback_to_full_download = false; "
	            "SET external_file_cache_remote_max_block_size = 262144; SET enable_http_metadata_cache = true; "
	            "SET enable_logging = true; SET logging_level = 'DEBUG'; SET logging_mode = 'ENABLE_SELECTED'; "
	            "SET enabled_log_types = 'HTTP'; SELECT count(*) FROM " HTTP_LOG);
	if (token) {
		expect("true", "CREATE SECRET fixtures (TYPE http, BEARER_TOKEN '%s', SCOPE '%s')", token, scope);
	}

	expect("34", "SELECT len(queries) FROM read_json('%s')", manifest);
	char sql[4096];
	snprintf(sql, sizeof(sql),
	         "SELECT replace(replace(q.sql, '{dir}', '%s'), chr(10), ' '), q.expected "
	         "FROM (SELECT unnest(queries) AS q FROM read_json('%s'))",
	         dir, manifest);
	duckdb_result queries;
	if (duckdb_query(con, sql, &queries) != DuckDBSuccess) {
		fail_error(sql, &queries, "");
		duckdb_destroy_result(&queries);
		return;
	}
	idx_t count = duckdb_row_count(&queries);
	// Each expected value is the result cast to VARCHAR. The cast is in SQL,
	// because duckdb_value_varchar returns NULL for a list.
	for (idx_t i = 0; i < count && !host_failed(before); i++) {
		char *query = duckdb_value_varchar(&queries, 0, i), *expected = duckdb_value_varchar(&queries, 1, i);
		expect(expected, "SELECT (%s)::VARCHAR", query);
		duckdb_free(query);
		duckdb_free(expected);
	}
	if (host_failed(before)) {
		duckdb_destroy_result(&queries);
		return;
	}

	// Every GET asked for a byte range and got only that range.
	expect("0", "SELECT count(*) FROM " HTTP_LOG " WHERE request.type = 'GET' "
	            "AND (request.headers['Range'] IS NULL OR response.status = 'OK_200')");
	report_requests("for the fixture queries");

	// Repeated reads: the two queries that look up one id in
	// multi_row_group.parquet run again with the external file cache off. They
	// must fetch from the host again, and only part of the file, because each
	// needs only the row group that holds its id. Clearing the log first counts
	// their requests alone. Without the cache off, how much the first pass
	// fetched depends on the cache, which doesn't reuse blocks of a file whose
	// Last-Modified is less than 10 s old, such as a fresh checkout's.
	expect("0", "SET enable_external_file_cache = false; SELECT count(*) FROM truncate_duckdb_logs()");
	int repeated = 0;
	for (idx_t i = 0; i < count && !host_failed(before); i++) {
		char *query = duckdb_value_varchar(&queries, 0, i), *expected = duckdb_value_varchar(&queries, 1, i);
		if (strstr(query, "multi_row_group.parquet') WHERE id = ")) {
			expect(expected, "SELECT (%s)::VARCHAR", query);
			repeated++;
		}
		duckdb_free(query);
		duckdb_free(expected);
	}
	duckdb_destroy_result(&queries);
	if (host_failed(before)) {
		return;
	}
	if (repeated != 2) {
		char message[128];
		snprintf(message, sizeof(message), "found %d queries that look up one id, expected 2", repeated);
		fail("repeated reads of multi_row_group.parquet", message);
	}
	expect("true",
	       "SELECT count(*) > 0 AND sum(r.last_byte - r.first_byte + 1) < (SELECT f.size FROM "
	       "(SELECT unnest(files) AS f FROM read_json('%s')) WHERE f.path = 'multi_row_group.parquet') "
	       "FROM " GET_RANGES " WHERE url = '%s/multi_row_group.parquet'",
	       manifest, dir);

	char *absent = value_of("SELECT absent FROM read_json('%s')", manifest);
	if (absent) {
		expect_http_error(404, "SELECT * FROM read_parquet('%s/%s')", dir, absent);
	}
	duckdb_free(absent);
	report_requests("for the repeated reads and the missing file");
}
#endif

// Reports the checks and returns the exit status described at the top.
static int finish(void) {
	if (failures) {
		printf("%d of %d checks failed, %d of them in the host or network\n", failures, passes + failures,
		       host_failures);
		return failures == host_failures ? 3 : 1;
	}
	if (!passes) {
		printf("no checks ran\n");
		return 1;
	}
	printf("%d checks passed\n", passes);
	return 0;
}

int main(int argc, char **argv) {
	if (argc != 5) {
		fprintf(stderr,
		        "Usage: %s <expected version> <data dir> <host:port> <fixtures dir>\n"
		        "       %s <expected version> --remote <base URL> <fixtures dir>\n",
		        argv[0], argv[0]);
		return 2;
	}
	const char *version = argv[1], *dir = argv[2], *endpoint = argv[3], *fixtures = argv[4];
	const bool remote = strcmp(dir, "--remote") == 0;
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
	(void)endpoint, (void)fixtures;
	if (remote) {
		fprintf(stderr, "--remote needs a build that registers the extensions\n");
		return 2;
	}
	expect("0", "SELECT count(*) FROM duckdb_extensions() WHERE loaded");
	expect_error("core_functions", "SELECT to_minutes(5)");
	// Nothing registered parquet, and the bundle can't load it from anywhere else.
	expect_error(NO_LOADABLE, "LOAD parquet");
	expect_error("parquet", "COPY (SELECT 1 AS i) TO '%s/unlinked.parquet' (FORMAT parquet)", dir);
	expect_error("json", "SELECT json_extract_string('{\"a\": 1}', '$.a')");
#else
	if (remote) {
		// GITHUB_TOKEN goes into an SQL string and an HTTP header, so it can't
		// hold a quote, a space or a control character.
		token = getenv("GITHUB_TOKEN");
		if (token && !*token) {
			token = NULL;
		}
		for (const char *c = token; c && *c; c++) {
			if (*c == '\'' || (unsigned char)*c <= ' ' || *c == 0x7f) {
				token = NULL;
				printf("FAIL GITHUB_TOKEN has a quote, a space or a control character, so it can't be used\n");
				return 2;
			}
		}
		printf("info GITHUB_TOKEN is %s\n",
		       token ? "set: requests under " REMOTE_SCOPE " send it as a bearer token"
		             : "not set: the fixtures are read without credentials");
		check_fixtures(endpoint, fixtures, REMOTE_SCOPE);
		close_database();
		return finish();
	}

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

	// The fixture queries, with a token scoped to the server's copy of the
	// fixtures. smoke.sh checks that every request for them sent the token and
	// that the request outside the scope, below, didn't.
	char base[1000], scope[1024];
	snprintf(base, sizeof(base), "http://%s/public", endpoint);
	snprintf(scope, sizeof(scope), "%s/", base);
	token = "smoke-token";
	check_fixtures(base, fixtures, scope);
	expect_http_error(404, "SELECT * FROM read_parquet('http://%s/outside-scope.parquet')", endpoint);
#endif
	close_database();

#ifndef SMOKE_UNLINKED
	char path[4096];
	snprintf(path, sizeof(path), "%s/smoke.duckdb", dir);
	check_reopen(path);
#endif

	return finish();
}
