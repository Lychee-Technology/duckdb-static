#!/usr/bin/env bash
# Smoke-tests a release tarball: extracts it, links test/smoke.c against
# libduckdb_bundle.a with the link flags a consumer uses, and runs it.
#
# Usage: test/smoke.sh <libduckdb_bundle-*.tar.xz> <expected DuckDB version> <include dir>
#
# <include dir> holds the duckdb.h of the DuckDB version the bundle was built
# from. 1.5.x tarballs ship only the archive, so the build passes the header
# from its DuckDB checkout, and also uploads it as an artifact for local runs.
#
# Before building anything, it checks that the tarball holds exactly the
# archive, and that the archive is built for this host's architecture. Then it
# builds the program once (see test/smoke.c); DuckDB 1.5.x has no registration
# step to build variants for. The binary may only need the shared libraries on
# an allowlist below. It starts with an empty data directory and must report
# how many checks passed. The httpfs checks read from a local HTTP server, so
# the test needs no network access. CC defaults to cc; on Amazon Linux 2023 set
# it to the gcc14 the bundle was built with.
#
# The binary also runs the fixture queries in
# test/fixtures/public/v1/manifest.json against the local server. When
# DUCKDB_TEST_HTTPFS_BASE_URL is set, the same queries then run against that
# public HTTPS copy of test/fixtures/public, after test/fixtures/fixtures.py
# verify checks that it serves the committed files with working byte ranges.
# If GITHUB_TOKEN is set, those requests send it, but only to this repository's
# files on raw.githubusercontent.com. Unset, the remote run is reported as not
# configured, which is not a pass.
#
# Exits 0 if every check that ran passed, 3 if only the remote run failed and
# every failure was in the host or the network (DNS, TLS, connection, timeout,
# or an HTTP error status such as 429), and 1 if anything points at the bundle
# or the remote run's configuration can't be used. When the host answered 429,
# the summary at the end says so on a line of its own, starting "rate-limited:".
set -euo pipefail

if [ $# -ne 3 ]; then
	echo "Usage: $0 <libduckdb_bundle-*.tar.xz> <expected DuckDB version> <include dir>" >&2
	exit 1
fi
tarball=$1
version=$2
include=$3
here=$(cd "$(dirname "$0")" && pwd)
CC=${CC:-cc}

if [ ! -f "$include/duckdb.h" ]; then
	echo "FAIL $include has no duckdb.h"
	exit 1
fi

work=$(mktemp -d)
server_pid=
cleanup() {
	if [ -n "$server_pid" ]; then kill "$server_pid" 2>/dev/null || true; fi
	rm -rf "$work"
}
trap cleanup EXIT
mkdir -p "$work/bundle" "$work/data/bucket"
if ! tar -xJf "$tarball" -C "$work/bundle"; then
	echo "FAIL $tarball is not a readable .tar.xz"
	exit 1
fi
echo "Tarball contents:"
ls -l "$work/bundle"

# A 1.5.x release tarball holds exactly the archive, as every release since
# v1.5.3_1 has. Anything else changes what consumers extract.
expected_files=libduckdb_bundle.a
files=$(find "$work/bundle" -mindepth 1 -printf '%P\n' | LC_ALL=C sort)
if [ "$files" != "$expected_files" ]; then
	echo "FAIL the tarball holds:"
	sed 's/^/     /' <<<"$files"
	echo "     expected exactly:"
	sed 's/^/     /' <<<"$expected_files"
	exit 1
fi
echo "ok   tarball holds exactly: $expected_files"

# Every member of the archive must be built for this host's architecture. A
# mismatch means the tarball is for the other architecture, or the build mixed
# objects from two.
case $(uname -m) in
x86_64) machine='Advanced Micro Devices X86-64' ;;
aarch64) machine=AArch64 ;;
*)
	echo "FAIL no expected archive machine type for a $(uname -m) host"
	exit 1
	;;
esac
machines=$(readelf -h "$work/bundle/libduckdb_bundle.a" | sed -n 's/^ *Machine: *//p' | sort -u | paste -sd, -) || true
if [ "$machines" != "$machine" ]; then
	echo "FAIL libduckdb_bundle.a is built for '${machines:-no ELF objects}', expected '$machine' on this $(uname -m) host"
	exit 1
fi
echo "ok   libduckdb_bundle.a is built for $machine"

# Serves data/, and the committed fixtures under /public/, with byte ranges,
# which httpfs uses to read parquet, and logs each request with its
# Authorization scheme.
python3 -I "$here/fixtures/serve.py" --root "$work/data" --mount "public=$here/fixtures/public" \
	--port-file "$work/port" --log "$work/requests.log" &
server_pid=$!
for _ in $(seq 50); do
	[ -s "$work/port" ] && break
	sleep 0.1
done
endpoint="127.0.0.1:$(cat "$work/port")"

# A failure here means duckdb.h doesn't match the archive, or the archive lacks
# a symbol a consumer links, such as a dropped extension member.
if ! "$CC" -O2 -Wall -Werror -I"$include" "$here/smoke.c" -o "$work/smoke" \
	-L"$work/bundle" -lduckdb_bundle -lstdc++ -lm -lcurl -lssl -lcrypto -lpthread -ldl -Wl,--gc-sections; then
	echo "FAIL smoke doesn't build against $include/duckdb.h and the tarball's archive"
	exit 1
fi

# A binary linked against the bundle may need only these shared libraries:
# libstdc++, libm, libcurl, libssl and libcrypto from the link line above,
# plus libgcc_s, libc and the dynamic loader. -lpthread and -ldl add nothing,
# because glibc 2.34 moved both into libc. Libraries are matched by name without
# the soname version, so an Amazon Linux 2023 package update doesn't break the
# check. libduckdb is never on the list, because the bundle links DuckDB
# statically. Anything else is a new run-time dependency for every consumer.
check_needed() {
	local name=$1 lib needed ok=0
	needed=$(readelf -d "$work/$name" | sed -n 's/.*(NEEDED).*\[\(.*\)\]$/\1/p')
	if [ -z "$needed" ]; then
		echo "FAIL $name needs no shared libraries, expected at least libc"
		return 1
	fi
	for lib in $needed; do
		case ${lib%%.so*} in
		libstdc++ | libm | libcurl | libssl | libcrypto | libgcc_s | libc | ld-linux-*) ;;
		*)
			echo "FAIL $name needs $lib, which isn't on smoke.sh's allowlist"
			ok=1
			;;
		esac
	done
	if [ $ok -eq 0 ]; then
		echo "ok   $name needs only allowed libraries: $(paste -sd' ' <<<"$needed")"
	fi
	return $ok
}

status=0
check_needed smoke || status=1
if ! "$work/smoke" "$version" "$work/data" "$endpoint" "$here/fixtures/public" | tee "$work/smoke.log"; then
	status=1
# Guards against a binary that exits 0 without running its checks.
elif ! tail -n1 "$work/smoke.log" | grep -Eq '^[1-9][0-9]* checks passed$'; then
	echo "FAIL smoke did not report any passed checks"
	status=1
else
	# The reads must have reached the server: plain GETs for http://, signed ones
	# for s3://, and GETs of the fixtures with their bearer token. The request
	# outside the token's scope must not have sent it.
	for pattern in 'GET /bucket/smoke.parquet -' 'GET /bucket/smoke.parquet AWS4-HMAC-SHA256' \
		'GET /public/v1/multi_row_group.parquet Bearer' 'HEAD /outside-scope.parquet -'; do
		if ! grep -qF -- "$pattern" "$work/requests.log"; then
			echo "FAIL the server logged no '$pattern' request:"
			cat "$work/requests.log"
			status=1
		fi
	done
	if grep -E '^[A-Z]+ /public/' "$work/requests.log" | grep -v ' Bearer$'; then
		echo "FAIL the requests above read the fixtures without the token scoped to them"
		status=1
	fi
	if grep -Ev '^[A-Z]+ /public/' "$work/requests.log" | grep ' Bearer$'; then
		echo "FAIL the requests above sent the fixtures' token outside its scope"
		status=1
	fi
fi

# The same fixture queries against a public HTTPS host, when one is configured.
# fixtures.py verify checks first that the host serves the committed files with
# working byte ranges, so a host that doesn't fails the preflight rather than a
# query. Failures in the host or the network exit 3 rather than 1, so that they
# aren't taken for a regression in the bundle.
remote=${DUCKDB_TEST_HTTPFS_BASE_URL:-}
remote_status=0
if [ -z "$remote" ]; then
	remote_result="not configured: DUCKDB_TEST_HTTPFS_BASE_URL is unset, so nothing was read over HTTPS"
else
	echo "== preflight of $remote"
	# Only verify's exit 1, a failed check of the host, is a host failure. Its
	# 4 is a base URL that isn't one, and any other status is the script itself.
	preflight=0
	python3 -I "$here/fixtures/fixtures.py" verify "$remote" | tee "$work/preflight.log" || preflight=${PIPESTATUS[0]}
	if [ "$preflight" -eq 1 ]; then
		remote_status=3
		remote_result="failed the preflight: the host doesn't serve the fixtures as the test needs"
	elif [ "$preflight" -eq 4 ]; then
		remote_status=1
		remote_result="not run: DUCKDB_TEST_HTTPFS_BASE_URL isn't an http:// or https:// URL"
	elif [ "$preflight" -ne 0 ]; then
		remote_status=1
		remote_result="failed: fixtures.py verify exited $preflight"
	else
		echo "== smoke --remote $remote"
		code=0
		"$work/smoke" "$version" --remote "$remote" "$here/fixtures/public" | tee "$work/remote.log" ||
			code=${PIPESTATUS[0]}
		if [ "$code" -eq 3 ]; then
			remote_status=3
			remote_result="failed in the host or network, not in the bundle: see the FAIL lines labeled host: or network:"
		elif [ "$code" -eq 2 ]; then
			remote_status=1
			remote_result="not run: its configuration is invalid, see the FAIL line above"
		elif [ "$code" -ne 0 ]; then
			remote_status=1
			remote_result="failed: a FAIL line without a host: or network: label points at the bundle"
		elif ! tail -n1 "$work/remote.log" | grep -Eq '^[1-9][0-9]* checks passed$'; then
			remote_status=1
			remote_result="failed: smoke --remote did not report any passed checks"
		else
			remote_result="passed"
		fi
	fi
fi
echo "== summary"
if [ $status -eq 0 ]; then echo "local:  passed"; else echo "local:  failed"; fi
echo "remote: $remote_result"
# A 429 is a host failure like any other, but its own line lets CI count how
# often GitHub's rate limit stops the run. The patterns are fixtures.py
# verify's FAIL lines, pinned by test/fixtures/test_fixtures.py, and the label
# host_layer gives in smoke.c.
if grep -qsE '^FAIL .*: status 429,' "$work/preflight.log" || grep -qsF 'HTTP 429, rate-limited' "$work/remote.log"; then
	echo "rate-limited: the host answered HTTP 429"
fi
# 1 if anything points at the bundle, 3 if only the remote host or network failed
if [ $status -ne 0 ] || [ $remote_status -eq 1 ]; then
	exit 1
fi
exit $remote_status
