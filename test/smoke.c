// Links libduckdb_bundle.a the way a consumer does and runs a query against each
// bundled extension, comparing results rather than only checking for success.
// test/smoke.sh builds it three ways:
//
//   -DSMOKE_REGISTER   calls duckdb_register_static_extensions() before opening
//   (neither define)   is linked with -Wl,-u,duckdb_register_static_extensions
//                      and relies on the loader's before-main hook
//   -DSMOKE_UNLINKED   neither: expects the extensions to be missing, which
//                      shows that the registration, not a download, provides them
//
// Usage: smoke <expected version> <data dir> <host:port of test/smoke.sh's server>
// The server serves <data dir>, so a file written there can be read back over
// HTTP and, under bucket/, over S3.
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
// Nothing uses it yet.
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
#endif
	close_database();

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
