#!/usr/bin/env bash
# Packages a DuckDB 2.0 release build into libduckdb_bundle.a, the one static
# archive this repository releases, and writes the release tarball.
#
# Usage: scripts/package-bundle.sh <duckdb checkout> <extensions> <output .tar.xz>
#
# <extensions> is the build's semicolon-separated extension list. Its order is
# the order DuckDB loads the extensions in.
#
# DuckDB 2.0 builds the engine and each extension as separate archives, and
# libduckdb_static.a no longer registers extensions by itself. The bundle holds
# every member of libduckdb_static.a and of each extension archive, plus one
# object compiled from DuckDB's generated static extension loader and its
# before-main hook (extension/loader/). A consumer pulls that object in by
# calling duckdb_register_static_extensions() or by linking with
# -Wl,-u,duckdb_register_static_extensions, and the extensions are registered
# before main either way.
#
# CXX and CXXFLAGS must be the ones the build used, so the loader object is
# compiled for the same target.
set -euo pipefail

if [ $# -ne 3 ]; then
	echo "Usage: $0 <duckdb checkout> <extensions> <output .tar.xz>" >&2
	exit 1
fi
duckdb_dir=$(cd "$1" && pwd)
read -ra extensions <<<"${2//;/ }"
tarball=$(realpath -m "$3")
build_dir="$duckdb_dir/build/release"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/in" "$work/pkg"

# MRI scripts can't quote paths, so each input is linked under a plain name.
inputs=("$build_dir/src/libduckdb_static.a")
for name in "${extensions[@]}"; do
	found=$(find "$build_dir/extension" -name "lib${name}_extension.a")
	if [ -z "$found" ] || [ "$(wc -l <<<"$found")" -ne 1 ]; then
		echo "Expected one lib${name}_extension.a under $build_dir/extension, found: ${found:-none}" >&2
		exit 1
	fi
	inputs+=("$found")
done

python3 "$duckdb_dir/scripts/generate_static_extension_loader.py" \
	--output "$work/static_extension_loader.c" "${extensions[@]}"
# One translation unit, so the hook comes along whenever the loader is linked.
# Compiled into separate archive members, the linker would drop the hook.
cat >"$work/duckdb_bundle_loader.cpp" <<'EOF'
#include "static_extension_loader.c"
#include "static_extension_autoregister.cpp"
EOF
# shellcheck disable=SC2086 # CXXFLAGS holds several flags
"$CXX" $CXXFLAGS -Os -std=c++17 \
	-I"$work" -I"$duckdb_dir/extension/loader" -I"$duckdb_dir/src/include" \
	-c "$work/duckdb_bundle_loader.cpp" -o "$work/in/duckdb_bundle_loader.o"

# ar's MRI ADDLIB copies every member, including members that share a name.
# Extracting the archives to a directory and re-archiving it would keep only
# one of each name.
cd "$work"
expected_members=1
{
	echo "CREATE libduckdb_bundle.a"
	for i in "${!inputs[@]}"; do
		ln -s "${inputs[$i]}" "in/$i.a"
		echo "ADDLIB in/$i.a"
		expected_members=$((expected_members + $(ar t "${inputs[$i]}" | wc -l)))
	done
	echo "ADDMOD in/duckdb_bundle_loader.o"
	echo "SAVE"
	echo "END"
} >bundle.mri
ar -M <bundle.mri
objcopy --strip-debug libduckdb_bundle.a

members=$(ar t libduckdb_bundle.a | wc -l)
if [ "$members" -ne "$expected_members" ]; then
	echo "libduckdb_bundle.a has $members members, expected $expected_members" >&2
	exit 1
fi
defined=$(nm --defined-only libduckdb_bundle.a 2>/dev/null)
symbols=(duckdb_register_static_extensions duckdb_register_static_extension)
for name in "${extensions[@]}"; do
	symbols+=("duckdb_extension_${name}_describe")
done
for symbol in "${symbols[@]}"; do
	if ! grep -q " T ${symbol}\$" <<<"$defined"; then
		echo "libduckdb_bundle.a doesn't define $symbol" >&2
		exit 1
	fi
done
machines=$(readelf -h libduckdb_bundle.a | sed -n 's/^ *Machine: *//p' | sort -u)
if [ "$(wc -l <<<"$machines")" -ne 1 ]; then
	echo "libduckdb_bundle.a mixes machine types: $machines" >&2
	exit 1
fi
echo "libduckdb_bundle.a: $members members, $machines, registers ${extensions[*]}"

cp libduckdb_bundle.a "$duckdb_dir/src/include/duckdb.h" \
	"$duckdb_dir/src/include/duckdb_static_extension.h" pkg/
mtime=$(git -C "$duckdb_dir" log -1 --format=%ct)
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$mtime" \
	-cJf "$tarball" -C pkg .
ls -l pkg
echo "Wrote $tarball"
