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
#include "duckdb.h"

#include <stdarg.h>
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

static duckdb_connection con;
static int failures;

static void fail(const char *sql, const char *message) {
	printf("FAIL %s\n     %s\n", sql, message);
	failures++;
}

static char *format_sql(const char *format, va_list args) {
	static char sql[4096];
	vsnprintf(sql, sizeof(sql), format, args);
	return sql;
}

// Checks that the query succeeds and that the first value of its result is expected.
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
		char *got = duckdb_value_varchar(&result, 0, 0);
		if (!got || strcmp(got, expected) != 0) {
			char message[1024];
			snprintf(message, sizeof(message), "expected '%s', got '%s'", expected, got ? got : "NULL");
			fail(sql, message);
		} else {
			printf("ok   %s -> %s\n", sql, got);
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
		printf("ok   %s -> error containing '%s'\n", sql, fragment);
	}
	duckdb_destroy_result(&result);
}

int main(int argc, char **argv) {
	if (argc != 4) {
		fprintf(stderr, "Usage: %s <expected version> <data dir> <host:port>\n", argv[0]);
		return 2;
	}
	const char *version = argv[1], *dir = argv[2], *endpoint = argv[3];

#ifdef SMOKE_REGISTER
	if (duckdb_register_static_extensions() != 0) {
		fprintf(stderr, "duckdb_register_static_extensions failed\n");
		return 1;
	}
#endif
	duckdb_database db;
	if (duckdb_open(NULL, &db) != DuckDBSuccess || duckdb_connect(db, &con) != DuckDBSuccess) {
		fprintf(stderr, "could not open an in-memory database\n");
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

	duckdb_disconnect(&con);
	duckdb_close(&db);
	if (failures) {
		printf("%d check(s) failed\n", failures);
		return 1;
	}
	printf("all checks passed\n");
	return 0;
}
