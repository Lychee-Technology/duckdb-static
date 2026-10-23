#!/usr/bin/env bash
# Benchmarks DuckDB readiness and a first local Parquet query, with duckdb-go's
# stock library and with a bundle, under sam local invoke on provided.al2023.
# README.md says what is measured. Run it from anywhere on a host of the
# bundle's architecture:
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
# which and why.
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

echo '{}' >"$results/provenance.json"
# prov <key> <JSON value>: records a value in results/provenance.json.
prov() {
	jq --argjson v "$2" ".$1 = \$v" "$results/provenance.json" >"$results/provenance.tmp"
	mv "$results/provenance.tmp" "$results/provenance.json"
}
str() { jq -n --arg s "$1" '$s'; }

finish() {
	python3 -I "$here/summarize.py" "$results" "$repo/test/fixtures/public/v1/manifest.json"
}
# setup_failed <status> <reason>: ends the run before any sample.
setup_failed() {
	prov setup "$(jq -n --arg status "$1" --arg reason "$2" '{status: $status, reason: $reason}')"
	finish || exit $?
	exit 1
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

fixture=$repo/test/fixtures/public/v1/multi_row_group.parquet
sizes='{}'
for v in stock custom; do
	mkdir -p "$work/$v/data"
	cp "$work/out/bootstrap" "$work/$v/bootstrap"
	cp "$work/out/$v/duckdb-bench" "$work/$v/duckdb-bench"
	cp "$fixture" "$work/$v/data/"
	python3 -I -c 'import shutil, sys; shutil.make_archive(sys.argv[1], "zip", sys.argv[2])' "$work/$v" "$work/$v"
	sizes=$(jq --arg v "$v" --argjson exe "$(stat -c %s "$work/$v/duckdb-bench")" \
		--argjson pkg "$(du -sb "$work/$v" | cut -f1)" --argjson zip "$(stat -c %s "$work/$v.zip")" \
		'.[$v] = {executable_bytes: $exe, package_bytes: $pkg, package_zip_bytes: $zip}' <<<"$sizes")
done
prov sizes "$sizes"
cp "$here/template.yaml" "$work/template.yaml"
echo '{"mode": "bench"}' >"$work/event-bench.json"

pull=()
# sample <file> <variant> <event> <iteration> <position>: invokes the variant
# once, in a new container, and appends the response to results/<file>.
sample() {
	local fn=Stock out status=0
	[ "$2" = custom ] && fn=Custom
	out=$(sam local invoke "$fn" -t "$work/template.yaml" -e "$3" \
		--parameter-overrides "Architecture=$lambda_arch" "${pull[@]}" 2>>"$results/sam.log") || status=$?
	out=$(awk 'NF { last = $0 } END { print last }' <<<"$out")
	jq -cn --arg variant "$2" --argjson iteration "$4" --argjson position "$5" \
		--argjson status "$status" --arg raw "$out" \
		'($raw | try fromjson catch null) as $r
		| {variant: $variant, iteration: $iteration, position: $position, invoke_status: $status, response: $r}
		+ (if ($r | type) == "object" then {} else {raw: $raw} end)' >>"$results/$1"
}

# The first invocation of each variant builds SAM's runtime image. Its sample
# is kept apart, and later invocations don't check the registry again.
echo "== warm-up"
sample warmup.jsonl stock "$work/event-bench.json" 0 1
sample warmup.jsonl custom "$work/event-bench.json" 0 2
pull=(--skip-pull-image)

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

finish
