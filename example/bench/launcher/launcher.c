/*
 * The function's bootstrap. It reads CLOCK_MONOTONIC, passes the reading to
 * duckdb-bench in BENCH_BOOTSTRAP_MONOTONIC_NS, and execs it in the same
 * process. duckdb-bench reads the same clock first thing in main, so the
 * difference covers loading the executable and its shared libraries, static
 * constructors, the Go runtime and package initialization. DuckDB 1.5.6 has
 * nothing to register: it loads the linked extensions when a database opens.
 * It doesn't cover starting this launcher, which happens before the first
 * reading.
 */
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

int main(int argc, char **argv) {
	(void)argc;
	struct timespec ts;
	char ns[32];
	char path[4096];
	const char *root = getenv("LAMBDA_TASK_ROOT");

	snprintf(path, sizeof path, "%s/duckdb-bench", root ? root : "/var/task");
	clock_gettime(CLOCK_MONOTONIC, &ts);
	snprintf(ns, sizeof ns, "%lld", (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec);
	setenv("BENCH_BOOTSTRAP_MONOTONIC_NS", ns, 1);
	argv[0] = path;
	execv(path, argv);
	perror(path);
	return 127;
}
