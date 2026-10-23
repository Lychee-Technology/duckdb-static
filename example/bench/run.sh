#!/usr/bin/env bash
# Benchmarks DuckDB readiness and a first Parquet query over HTTP, with
# duckdb-go's stock library and with a bundle, under sam local invoke on
# provided.al2023. The query reads test/fixtures/public/v2/tiny.parquet from a
# fixture server (test/fixtures/serve.py) that this script runs in a container
# on a Docker network of its own, which the Lambda containers join. README.md
# says what is measured. Run it from anywhere on a host of the bundle's
# architecture:
#
#   BUNDLE_TARBALL=<bundle .tar.xz> example/bench/run.sh
#
# Environment:
#   BUNDLE_TARBALL               the bundle to test (required)
#   SAMPLES                      samples per variant (default 30)
#   BENCH_WORK                   work directory (default: a new temporary one);
#                                the results are in $BENCH_WORK/results
#   DUCKDB_TEST_HTTPFS_BASE_URL  if set, also check, untimed, that each variant
#                                reads the v1 fixtures from it over httpfs
#
# Exits 0 when the result is valid and 1 otherwise. results/summary.md says
# which and why. The fixture server's container and network are removed on
# exit, whatever the outcome.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
# shellcheck source=../../duckdb_version
. "$repo/duckdb_version"

case $(uname -m) in
x86_64) arch=amd64 lambda_arch=x86_64 ;;
aarch64) arch=arm64 lambda_arch=arm64 ;;
*)
	echo "run.sh: unsupported host architecture $(uname -m)" >&2
	exit 1
	;;
esac

: "${BUNDLE_TARBALL:?set BUNDLE_TARBALL to a bundle .tar.xz}"
samples=${SAMPLES:-30}
if ! [[ $samples =~ ^[1-9][0-9]*$ ]]; then
	echo "run.sh: SAMPLES must be a positive integer, not '$samples'" >&2
	exit 1
fi
work=${BENCH_WORK:-$(mktemp -d)}
results=$work/results
rm -rf "$work/stock" "$work/custom" "$work/out" "$results"
mkdir -p "$results"
export SAM_CLI_TELEMETRY=0

# The fixture server. The Lambda containers reach it by its alias on a network
# named for this run, so two runs on one host don't share a server.
fixture_image=public.ecr.aws/amazonlinux/amazonlinux:2023
fixture_version=v2
fixture_file=tiny.parquet
fixture_alias=fixtures
net=duckdb-bench-$(date +%s)-$$
server=$net-fixtures
fixture_url=http://$fixture_alias:8080/$fixture_version/$fixture_file
manifest=$repo/test/fixtures/public/$fixture_version/manifest.json
net_created=
server_started=
cleanup() {
	if [ -n "$server_started" ]; then docker rm -f "$server" >/dev/null 2>&1 || true; fi
	if [ -n "$net_created" ]; then docker network rm "$net" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo '{}' >"$results/provenance.json"
# prov <key> <JSON value>: records a value in results/provenance.json.
prov() {
	jq --argjson v "$2" ".$1 = \$v" "$results/provenance.json" >"$results/provenance.tmp"
	mv "$results/provenance.tmp" "$results/provenance.json"
}
str() { jq -n --arg s "$1" '$s'; }

# Saves the fixture server's request log, one JSON object per line, and its
# standard error.
save_server_log() {
	if [ -n "$server_started" ]; then
		docker logs "$server" >"$results/fixture-requests.jsonl" 2>"$results/fixture-server.log" || true
	fi
}
finish() {
	save_server_log
	python3 -I "$here/summarize.py" "$results" "$manifest"
}
# infra_failed <status> <reason>: records a failure of the setup or of the
# fixture server, which decides the run's status.
infra_failed() {
	prov setup "$(jq -n --arg status "$1" --arg reason "$2" '{status: $status, reason: $reason}')"
}
# setup_failed <status> <reason>: ends the run before any sample.
setup_failed() {
	infra_failed "$1" "$2"
	finish || exit $?
	exit 1
}
# verify_fixtures <log>: checks, from a container on the run's network, that
# the fixture server serves the committed v2 files with working byte ranges
# (test/fixtures/fixtures.py verify).
verify_fixtures() {
	docker run --rm --network "$net" --security-opt label=disable \
		-v "$repo/test/fixtures:/fixtures:ro" "$fixture_image" \
		python3 -I /fixtures/fixtures.py verify "http://$fixture_alias:8080" --version "$fixture_version" \
		>"$1" 2>&1
}

prov arch "$(str "$arch")"
prov duckdb_version "$(str "$DUCKDB_VER")"
prov samples_requested "$samples"
prov runner "$(jq -n --arg kernel "$(uname -srm)" --arg cpus "$(nproc)" \
	--arg cpu "$(LC_ALL=C lscpu 2>/dev/null | sed -n 's/^Model name: *//p' | head -n1)" \
	--arg docker "$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)" \
	--arg sam "$(sam --version 2>/dev/null || true)" \
	'{kernel: $kernel, cpus: ($cpus | tonumber), cpu: $cpu, docker: $docker, sam: $sam}')"
prov lambda "$(jq -n '{runtime: "provided.al2023", memory_mb: 512, timeout_s: 30}')"

echo "== bundle $BUNDLE_TARBALL"
prov bundle "$(jq -n --arg name "$(basename "$BUNDLE_TARBALL")" \
	--arg sha256 "$(sha256sum "$BUNDLE_TARBALL" | cut -d' ' -f1)" '{name: $name, sha256: $sha256}')"
rm -rf "$here/libs"
mkdir -p "$here/libs"
tar -xJf "$BUNDLE_TARBALL" -C "$here/libs"

echo "== building both variants"
if ! docker build --progress=plain --build-arg "GO_ARCH=$arch" --target exporter \
	--output "type=local,dest=$work/out" -f "$here/Dockerfile" "$here/.." >"$results/build.log" 2>&1; then
	tail -n 40 "$results/build.log"
	setup_failed "not run" "Building the variants failed; see build.log."
fi
prov build "$(jq -Rs 'split("\n") | map(select(length > 0))' "$work/out/build-info.txt")"

stock_version=$(jq -r .duckdb_version "$work/out/stock/libduckdb-provenance.json")
prov stock_library "$(jq -c . "$work/out/stock/libduckdb-provenance.json")"
if [ "$stock_version" != "$DUCKDB_VER" ]; then
	setup_failed incomparable "The stock duckdb-go library in example/go.mod is DuckDB $stock_version, but duckdb_version is $DUCKDB_VER."
fi

# The stock library has no httpfs. Provision DuckDB's own build of it in the
# package, the way a stock deployment would, so that the timed interval only
# loads it from local disk.
ext_url=https://extensions.duckdb.org/$DUCKDB_VER/linux_$arch/httpfs.duckdb_extension.gz
ext_dir=$work/stock/ext/$DUCKDB_VER/linux_$arch
mkdir -p "$ext_dir" "$work/custom/ext"
echo "== provisioning $ext_url"
if ! download=$(curl -fsS -o "$work/httpfs.duckdb_extension.gz" -w '%{time_total}' "$ext_url"); then
	setup_failed incomparable "The stock variant can't load httpfs offline: downloading $ext_url failed."
fi
gunzip -c "$work/httpfs.duckdb_extension.gz" >"$ext_dir/httpfs.duckdb_extension"
prov stock_httpfs "$(jq -n --arg url "$ext_url" --argjson seconds "$download" \
	--argjson gz "$(stat -c %s "$work/httpfs.duckdb_extension.gz")" \
	--argjson bytes "$(stat -c %s "$ext_dir/httpfs.duckdb_extension")" \
	--arg sha256 "$(sha256sum "$ext_dir/httpfs.duckdb_extension" | cut -d' ' -f1)" \
	'{url: $url, download_seconds: $seconds, gz_bytes: $gz, bytes: $bytes, sha256: $sha256}')"

sizes='{}'
for v in stock custom; do
	cp "$work/out/bootstrap" "$work/$v/bootstrap"
	cp "$work/out/$v/duckdb-bench" "$work/$v/duckdb-bench"
	python3 -I -c 'import shutil, sys; shutil.make_archive(sys.argv[1], "zip", sys.argv[2])' "$work/$v" "$work/$v"
	sizes=$(jq --arg v "$v" --argjson exe "$(stat -c %s "$work/$v/duckdb-bench")" \
		--argjson pkg "$(du -sb "$work/$v" | cut -f1)" --argjson zip "$(stat -c %s "$work/$v.zip")" \
		'.[$v] = {executable_bytes: $exe, package_bytes: $pkg, package_zip_bytes: $zip}' <<<"$sizes")
done
prov sizes "$sizes"
cp "$here/template.yaml" "$work/template.yaml"
echo '{"mode": "bench"}' >"$work/event-bench.json"

# The fixture server runs before the first sample and until the last, on a
# network of the run's own. Its log has one line per request, with the query
# string that names the sample's process.
echo "== starting the fixture server on network $net"
fixture=$repo/test/fixtures/public/$fixture_version/$fixture_file
prov fixture "$(jq -n --arg url "$fixture_url" --arg path "$fixture_version/$fixture_file" \
	--argjson bytes "$(stat -c %s "$fixture")" --arg sha256 "$(sha256sum "$fixture" | cut -d' ' -f1)" \
	'{url: $url, path: $path, bytes: $bytes, sha256: $sha256}')"
if ! docker network create "$net" >"$results/fixture-server.log" 2>&1; then
	setup_failed "not run" "Creating the Docker network $net for the fixture server failed; see fixture-server.log."
fi
net_created=1
if ! docker run -d --name "$server" --network "$net" --network-alias "$fixture_alias" \
	--security-opt label=disable -v "$repo/test/fixtures:/fixtures:ro" "$fixture_image" \
	python3 -I -u /fixtures/serve.py --root /fixtures/public --bind 0.0.0.0 --port 8080 --json-log - \
	>>"$results/fixture-server.log" 2>&1; then
	setup_failed "not run" "Starting the fixture server failed; see fixture-server.log."
fi
server_started=1
for _ in $(seq 100); do
	docker logs "$server" 2>&1 | grep -q '^serving on ' && break
	sleep 0.1
done
prov fixture_server "$(jq -n --arg image "$fixture_image" \
	--arg image_id "$(docker inspect -f '{{.Image}}' "$server" 2>/dev/null || true)" --arg network "$net" \
	--arg alias "$fixture_alias" '{image: $image, image_id: $image_id, network: $network, alias: $alias}')"
if ! verify_fixtures "$results/fixture-preflight.log"; then
	cat "$results/fixture-preflight.log"
	setup_failed "not run" "The fixture server failed its check from a container on $net before any sample; see fixture-preflight.log."
fi
echo "fixture server serves $fixture_url"

pull=()
# sample <file> <variant> <event> <iteration> <position>: invokes the variant
# once, in a new container on the fixture server's network, and appends the
# response to results/<file>.
sample() {
	local fn=Stock out status=0
	[ "$2" = custom ] && fn=Custom
	out=$(sam local invoke "$fn" -t "$work/template.yaml" -e "$3" --docker-network "$net" \
		--parameter-overrides "Architecture=$lambda_arch FixtureUrl=$fixture_url" "${pull[@]}" \
		2>>"$results/sam.log") || status=$?
	out=$(awk 'NF { last = $0 } END { print last }' <<<"$out")
	jq -cn --arg variant "$2" --argjson iteration "$4" --argjson position "$5" \
		--argjson status "$status" --arg raw "$out" \
		'($raw | try fromjson catch null) as $r
		| {variant: $variant, iteration: $iteration, position: $position, invoke_status: $status, response: $r}
		+ (if ($r | type) == "object" then {} else {raw: $raw} end)' >>"$results/$1"
}

# The first invocation of each variant builds SAM's runtime image. Its sample
# is kept apart, and later invocations don't check the registry again. It is
# also the last check before sampling: each variant must read the fixture over
# HTTP and get the right sum.
echo "== warm-up"
sample warmup.jsonl stock "$work/event-bench.json" 0 1
sample warmup.jsonl custom "$work/event-bench.json" 0 2
pull=(--skip-pull-image)
for v in stock custom; do
	warmup=$(jq -c --arg v "$v" 'select(.variant == $v) | .response' "$results/warmup.jsonl")
	if ! jq -e '(type == "object") and .result_ok == true and (.error // "") == ""' <<<"$warmup" >/dev/null; then
		why=$(jq -r 'if type == "object" then (.error // .errorMessage // "") as $e
			| if $e != "" then $e else "sum(id) was \(.result)" end else "no response; see sam.log" end' <<<"$warmup")
		setup_failed "not run" "The $v variant's warm-up didn't read $fixture_url correctly, so no sample was taken: $why. The fixture server had passed its check from a container on the same network, so the cause is the variant or the Lambda container's network."
	fi
done

# Alternate the order, stock first in odd iterations and custom first in even
# ones, so that a drift on the host affects both variants alike.
echo "== $samples samples per variant"
for ((i = 1; i <= samples; i++)); do
	if ((i % 2)); then order=(stock custom); else order=(custom stock); fi
	sample samples.jsonl "${order[0]}" "$work/event-bench.json" "$i" 1
	sample samples.jsonl "${order[1]}" "$work/event-bench.json" "$i" 2
	echo "sample $i/$samples"
done

if [ -n "${DUCKDB_TEST_HTTPFS_BASE_URL:-}" ]; then
	echo "== HTTPS check against $DUCKDB_TEST_HTTPFS_BASE_URL"
	jq -n --arg url "${DUCKDB_TEST_HTTPFS_BASE_URL%/}/v1/multi_row_group.parquet" \
		'{mode: "remote", url: $url}' >"$work/event-remote.json"
	sample remote.jsonl stock "$work/event-remote.json" 1 1
	sample remote.jsonl custom "$work/event-remote.json" 1 2
fi

# A server that stopped or changed during the samples would make their HTTP
# timings meaningless.
echo "== checking the fixture server again"
if [ "$(docker inspect -f '{{.State.Running}}' "$server" 2>/dev/null)" != true ]; then
	infra_failed "invalid samples" "The fixture server's container wasn't running after the samples; see fixture-server.log."
elif ! verify_fixtures "$results/fixture-postcheck.log"; then
	cat "$results/fixture-postcheck.log"
	infra_failed "invalid samples" "The fixture server failed its check after the samples; see fixture-postcheck.log."
fi

finish
