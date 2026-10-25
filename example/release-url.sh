#!/bin/sh
# Prints the download URL of one architecture's bundle in the newest release of
# Lychee-Technology/duckdb-static for a DuckDB version: the release tagged
# <version>_<n> with the highest n. Drafts and prereleases don't count.
#
# Usage: release-url.sh <DuckDB version> <arch> <asset flavor>
#   e.g. release-url.sh v1.5.6 arm64 httpfs-parquet
#
# The release marked Latest can hold another DuckDB version, such as main's 2.0
# while this branch builds 1.5.x, so the version picks the release.
#
# Exits 2 if no release has a <version>_<n> tag, as between changing
# duckdb_version and releasing that version, and 1 on any other failure.
set -eu

if [ $# -ne 3 ] || [ -z "$1" ] || [ -z "$2" ] || [ -z "$3" ]; then
	echo "usage: $0 <DuckDB version> <arch> <asset flavor>" >&2
	exit 1
fi
version=$1 arch=$2 flavor=$3

api=https://api.github.com/repos/Lychee-Technology/duckdb-static/releases
releases='[]'
page=1
while :; do
	if ! json=$(curl -fsSL "$api?per_page=100&page=$page"); then
		echo "Listing the releases at $api failed" >&2
		exit 1
	fi
	releases=$(printf '%s\n%s\n' "$releases" "$json" | jq -cs add)
	[ "$(printf '%s' "$json" | jq length)" -lt 100 ] && break
	page=$((page + 1))
done

tag=$(printf '%s' "$releases" | jq -r --arg prefix "${version}_" '
	[.[] | select((.draft or .prerelease) | not)
	     | select(.tag_name | startswith($prefix) and (ltrimstr($prefix) | test("^[0-9]+$")))]
	| max_by(.tag_name | ltrimstr($prefix) | tonumber) | .tag_name // empty')
if [ -z "$tag" ]; then
	echo "No release is tagged ${version}_<n>. To use a bundle you built, set BUNDLE_TARBALL to its .tar.xz." >&2
	exit 2
fi

# The name only has to contain the tarball's: example/Makefile has always matched
# assets this way, so the release never publishes a name like <tarball>.sha256.
pattern="libduckdb_bundle-${arch}-linux-${flavor}\\.tar"
url=$(printf '%s' "$releases" | jq -r --arg tag "$tag" --arg p "$pattern" '
	.[] | select(.tag_name == $tag) | .assets[] | select(.name | test($p; "i")) | .browser_download_url' | head -n1)
if [ -z "$url" ]; then
	echo "Release $tag has no asset matching $pattern" >&2
	exit 1
fi
echo "Newest ${version}_<n> release: $tag" >&2
echo "$url"
