#!/usr/bin/env bash
# Smoke-tests a release tarball: extracts it, links test/smoke.c against
# libduckdb_bundle.a with the link flags a consumer uses, and runs it.
#
# Usage: test/smoke.sh <libduckdb_bundle-*.tar.xz> <expected DuckDB version>
#
# Before building anything, it checks that the tarball holds exactly the archive
# and its two headers, and that the archive is built for this host's
# architecture. Then it builds the program three ways (see test/smoke.c):
# registering the extensions with a call, registering them with a link flag,
# and not registering them, which must leave them missing. Each binary starts
# with an empty data directory and must report how many checks passed. The
# httpfs checks read from a local HTTP server, so the test needs no network
# access. CC defaults to cc; on Amazon Linux 2023 set it to the gcc14 the bundle
# was built with.
set -euo pipefail

if [ $# -ne 2 ]; then
	echo "Usage: $0 <libduckdb_bundle-*.tar.xz> <expected DuckDB version>" >&2
	exit 1
fi
tarball=$1
version=$2
here=$(cd "$(dirname "$0")" && pwd)
CC=${CC:-cc}

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

# The release tarball holds exactly the archive and the two public headers.
# Anything else means scripts/package-bundle.sh packaged the wrong files.
expected_files=$'duckdb.h\nduckdb_static_extension.h\nlibduckdb_bundle.a'
files=$(find "$work/bundle" -mindepth 1 -printf '%P\n' | LC_ALL=C sort)
if [ "$files" != "$expected_files" ]; then
	echo "FAIL the tarball holds:"
	sed 's/^/     /' <<<"$files"
	echo "     expected exactly:"
	sed 's/^/     /' <<<"$expected_files"
	exit 1
fi
echo "ok   tarball holds exactly: ${expected_files//$'\n'/ }"

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

# Serves data/ with byte ranges, which httpfs uses to read parquet, and logs
# each request with its Authorization scheme.
python3 -I - "$work/data" "$work/port" "$work/requests.log" <<'EOF' &
import http.server, os, re, sys

root, port_file, log_file = sys.argv[1:4]

class Handler(http.server.BaseHTTPRequestHandler):
    def respond(self, send_body):
        auth = self.headers.get('Authorization', '').split(' ', 1)[0] or '-'
        with open(log_file, 'a') as log:
            log.write(f'{self.command} {self.path} {auth}\n')
        path = os.path.join(root, self.path.split('?', 1)[0].lstrip('/'))
        if not os.path.isfile(path):
            self.send_error(404)
            return
        size = os.path.getsize(path)
        start, end = 0, size - 1
        match = re.fullmatch(r'bytes=(\d+)-(\d*)', self.headers.get('Range', ''))
        if match:
            start = int(match.group(1))
            end = min(int(match.group(2) or end), end)
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        else:
            self.send_response(200)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Last-Modified', self.date_time_string(int(os.path.getmtime(path))))
        self.end_headers()
        if send_body:
            with open(path, 'rb') as f:
                f.seek(start)
                self.wfile.write(f.read(end - start + 1))

    def do_HEAD(self):
        self.respond(False)

    def do_GET(self):
        self.respond(True)

    def log_message(self, format, *args):
        pass

server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
with open(port_file + '.tmp', 'w') as f:
    f.write(str(server.server_port))
os.rename(port_file + '.tmp', port_file)
server.serve_forever()
EOF
server_pid=$!
for _ in $(seq 50); do
	[ -s "$work/port" ] && break
	sleep 0.1
done
endpoint="127.0.0.1:$(cat "$work/port")"

libs=(-L"$work/bundle" -lduckdb_bundle -lstdc++ -lm -lcurl -lssl -lcrypto -lpthread -ldl)
build() {
	local name=$1
	shift
	"$CC" -O2 -Wall -Werror -I"$work/bundle" "$here/smoke.c" -o "$work/$name" "$@" "${libs[@]}" -Wl,--gc-sections
}
build smoke-register -DSMOKE_REGISTER
build smoke-linkflag -Wl,-u,duckdb_register_static_extensions
build smoke-unlinked -DSMOKE_UNLINKED

status=0
for name in smoke-register smoke-linkflag smoke-unlinked; do
	echo "== $name"
	rm -rf "$work/data"
	mkdir -p "$work/data/bucket"
	: >"$work/requests.log"
	if ! "$work/$name" "$version" "$work/data" "$endpoint" | tee "$work/$name.log"; then
		status=1
		continue
	fi
	# Guards against a binary that exits 0 without running its checks.
	if ! tail -n1 "$work/$name.log" | grep -Eq '^[1-9][0-9]* checks passed$'; then
		echo "FAIL $name did not report any passed checks"
		status=1
		continue
	fi
	if [ "$name" != smoke-unlinked ]; then
		# The reads must have reached the server: plain GETs for http://, signed
		# ones for s3://.
		for pattern in 'GET /bucket/smoke.parquet -' 'GET /bucket/smoke.parquet AWS4-HMAC-SHA256'; do
			if ! grep -qF -- "$pattern" "$work/requests.log"; then
				echo "FAIL the server logged no '$pattern' request:"
				cat "$work/requests.log"
				status=1
			fi
		done
	fi
done
exit $status
