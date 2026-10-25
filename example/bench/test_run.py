#!/usr/bin/env python3
"""Tests, with a stand-in for the docker command, that run.sh stops before any
sample when the variants can't be compared, and that it removes the fixture
server's container and network when setting them up fails.

Usage: python3 example/bench/test_run.py [-v]
"""

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DUCKDB_VER = next(
    line.split("=", 1)[1].strip()
    for line in (REPO / "duckdb_version").read_text().splitlines()
    if line.startswith("DUCKDB_VER=")
)


def bindings_version(duckdb):
    """duckdb-go-bindings' version for a DuckDB version: v1.5.6 is v0.10506.0."""
    major, minor, patch = (int(p) for p in duckdb.lstrip("v").split("."))
    return f"v0.{major * 10000 + minor * 100 + patch}.0"

# Keeps networks and containers as files under $FAKE_DOCKER_STATE, and acts
# as Docker and Podman do where run.sh's cleanup depends on it: a container
# whose start fails stays created, and a network with a container on it can't
# be removed. $FAKE_DOCKER_FAIL names the step that fails. The build writes
# what the Dockerfile writes: the stock executable links
# $FAKE_STOCK_MODULE_VERSION of the stock library, or no version of it when
# that is empty, and the variant $FAKE_DYNAMIC needs libduckdb.so.
FAKE_DOCKER = r"""#!/usr/bin/env bash
set -u
state=$FAKE_DOCKER_STATE
echo "$*" >>"$state/calls"
cmd=$1
shift
case $cmd in
version) exit 1 ;;
build)
	for a in "$@"; do
		case $a in type=local,dest=*) out=${a#type=local,dest=} ;; esac
	done
	mkdir -p "$out/stock" "$out/custom"
	echo "fake build" >"$out/build-info.txt"
	deps='{"Path": "github.com/duckdb/duckdb-go/v2", "Version": "v2.10506.0", "Sum": "h1:fake"}'
	echo "{\"Deps\": [$deps]}" >"$out/custom/buildinfo.json"
	if [ -n "$FAKE_STOCK_MODULE_VERSION" ]; then
		deps="$deps, {\"Path\": \"github.com/duckdb/duckdb-go-bindings/lib/linux-$FAKE_ARCH\", \"Version\": \"$FAKE_STOCK_MODULE_VERSION\", \"Sum\": \"h1:fake\"}"
	fi
	echo "{\"Deps\": [$deps]}" >"$out/stock/buildinfo.json"
	printf '%064d  libduckdb_static.a\n' 0 >"$out/stock/library.sha256"
	for v in stock custom; do
		printf 'libstdc++.so.6\nlibc.so.6\n' >"$out/$v/needed.txt"
		if [ "$FAKE_DYNAMIC" = "$v" ]; then echo libduckdb.so >>"$out/$v/needed.txt"; fi
	done
	touch "$out/bootstrap" "$out/stock/duckdb-bench" "$out/custom/duckdb-bench"
	;;
network)
	case $1 in
	create)
		if [ "$FAKE_DOCKER_FAIL" = network ]; then echo "Error: injected network failure" >&2; exit 1; fi
		touch "$state/net-$2"
		echo "$2"
		;;
	rm)
		if grep -qx -- "$2" "$state"/ctr-* 2>/dev/null; then echo "Error: network is being used" >&2; exit 2; fi
		rm "$state/net-$2" 2>/dev/null || { echo "Error: no such network" >&2; exit 1; }
		;;
	esac
	;;
run)
	name='' net='' detach=''
	while [ $# -gt 0 ]; do
		case $1 in
		-d) detach=1 ;;
		--name) name=$2; shift ;;
		--network) net=$2; shift ;;
		esac
		shift
	done
	if [ -z "$detach" ]; then
		# fixtures.py verify, in a --rm container.
		[ "$FAKE_DOCKER_FAIL" = preflight ] && { echo "injected verify failure"; exit 1; }
		exit 0
	fi
	echo "$net" >"$state/ctr-$name"
	if [ "$FAKE_DOCKER_FAIL" = server ]; then
		echo "Error: crun: injected OCI runtime failure" >&2
		exit 127
	fi
	touch "$state/started-$name"
	echo "fake-container-id"
	;;
logs)
	if [ -e "$state/started-$1" ]; then echo "serving on 0.0.0.0:8080" >&2; fi
	;;
inspect) echo "sha256:fake" ;;
rm)
	[ "$1" = -f ] && shift
	rm -f "$state/ctr-$1" "$state/started-$1"
	;;
*) echo "fake docker: unexpected command $cmd" >&2; exit 1 ;;
esac
"""

# Stands in for downloading the stock httpfs, which fails when
# $FAKE_DOCKER_FAIL is httpfs.
FAKE_CURL = r"""#!/usr/bin/env bash
set -eu
if [ "$FAKE_DOCKER_FAIL" = httpfs ]; then echo "curl: (22) The requested URL returned error: 404" >&2; exit 22; fi
while [ $# -gt 0 ]; do
	case $1 in -o) out=$2; shift ;; esac
	shift
done
echo fake | gzip -c >"$out"
printf 0.01
"""


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        # A copy of the scripts, because run.sh extracts the bundle next to
        # itself.
        tree = self.tmp / "tree"
        (tree / "example" / "bench").mkdir(parents=True)
        for name in ("run.sh", "summarize.py", "template.yaml"):
            shutil.copy2(HERE / name, tree / "example" / "bench" / name)
        shutil.copy2(REPO / "duckdb_version", tree / "duckdb_version")
        (tree / "test").symlink_to(REPO / "test")
        self.run_sh = tree / "example" / "bench" / "run.sh"

        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, text in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.state = self.tmp / "state"
        self.state.mkdir()

        self.bundle = self.tmp / "bundle.tar.xz"
        (self.tmp / "duckdb.h").write_text("")
        with tarfile.open(self.bundle, "w:xz") as tar:
            tar.add(self.tmp / "duckdb.h", arcname="duckdb.h")

        self.env = {
            k: v for k, v in os.environ.items()
            if k not in ("GITHUB_STEP_SUMMARY", "DUCKDB_TEST_HTTPFS_BASE_URL", "SAMPLES")
        }
        self.env.update(
            PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            BUNDLE_TARBALL=str(self.bundle),
            BENCH_WORK=str(self.tmp / "work"),
            FAKE_DOCKER_STATE=str(self.state),
            FAKE_DOCKER_FAIL="",
            FAKE_ARCH={"x86_64": "amd64", "aarch64": "arm64"}[os.uname().machine],
            FAKE_STOCK_MODULE_VERSION=bindings_version(DUCKDB_VER),
            FAKE_DYNAMIC="",
        )
        self.results = self.tmp / "work" / "results"

    def run_failing(self, status="not run", **env):
        proc = subprocess.run(
            ["bash", str(self.run_sh)], env={**self.env, **env},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120,
        )
        calls = (self.state / "calls").read_text()
        detail = f"run.sh output:\n{proc.stdout}\ndocker calls:\n{calls}"
        self.assertEqual(proc.returncode, 1, detail)
        left = sorted(p.name for p in self.state.iterdir() if p.name != "calls")
        self.assertEqual(left, [], detail)
        summary = json.loads((self.results / "summary.json").read_text())
        self.assertEqual(summary["status"], status, detail)
        return summary, calls

    def run_failing_at(self, step):
        return self.run_failing(FAKE_DOCKER_FAIL=step)

    def run_incomparable(self, reason, **env):
        """Checks that run.sh stops before starting the fixture server."""
        summary, calls = self.run_failing("incomparable", **env)
        self.assertNotIn("network create ", calls)
        self.assertIn(reason, summary["reasons"][0])
        return summary

    def test_stock_library_for_another_duckdb_version(self):
        other = "v0.10505.0" if DUCKDB_VER != "v1.5.5" else "v0.10506.0"
        self.run_incomparable(", but duckdb_version is " + DUCKDB_VER, FAKE_STOCK_MODULE_VERSION=other)
        prov = json.loads((self.results / "provenance.json").read_text())
        self.assertEqual(prov["stock_library"]["version"], other)
        self.assertFalse(prov["stock_library"]["provenance_json"])

    def test_stock_executable_without_the_stock_library(self):
        self.run_incomparable("The stock executable doesn't link github.com/duckdb/duckdb-go-bindings/lib/linux-",
                              FAKE_STOCK_MODULE_VERSION="")

    def test_variant_that_loads_duckdb_as_a_shared_library(self):
        for v in ("stock", "custom"):
            with self.subTest(variant=v):
                self.run_incomparable(f"The {v} executable loads DuckDB as a shared library (libduckdb.so)",
                                      FAKE_DYNAMIC=v)

    def test_stock_httpfs_unavailable(self):
        self.run_incomparable("The stock variant can't load httpfs offline", FAKE_DOCKER_FAIL="httpfs")

    def test_stock_library_for_this_duckdb_version_is_recorded(self):
        # Fails later, at the fixture server, after the checks above passed.
        self.run_failing_at("preflight")
        lib = json.loads((self.results / "provenance.json").read_text())["stock_library"]
        self.assertEqual((lib["duckdb_version"], lib["version"]), (DUCKDB_VER, bindings_version(DUCKDB_VER)))
        self.assertEqual(lib["archives"], [{"sha256": "0" * 64, "name": "libduckdb_static.a"}])

    def test_http_base_url_for_the_https_check(self):
        for url in ("http://example.com/fixtures", "HTTP://example.com/fixtures", "example.com/fixtures"):
            with self.subTest(url=url):
                proc = subprocess.run(
                    ["bash", str(self.run_sh)], env={**self.env, "DUCKDB_TEST_HTTPFS_BASE_URL": url},
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120,
                )
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn("DUCKDB_TEST_HTTPFS_BASE_URL must be an https:// URL", proc.stdout)
                self.assertFalse((self.state / "calls").exists(), "run.sh called docker")

    def test_https_base_url_in_any_case(self):
        # Gets past the URL check and fails later, at the fixture server.
        self.run_failing(FAKE_DOCKER_FAIL="preflight", DUCKDB_TEST_HTTPFS_BASE_URL="HTTPS://example.com/fixtures")

    def test_network_create_fails(self):
        summary, calls = self.run_failing_at("network")
        self.assertIn("network create ", calls)
        self.assertNotIn("run -d", calls)
        self.assertTrue(summary["reasons"][0].startswith("Creating the Docker network"), summary)

    def test_server_start_fails_after_creating_its_container(self):
        summary, calls = self.run_failing_at("server")
        self.assertIn("run -d --name ", calls)
        self.assertIn("rm -f ", calls)
        self.assertTrue(summary["reasons"][0].startswith("Starting the fixture server failed"), summary)
        # The reason points at this log, so it must keep the engine's error.
        self.assertIn("injected OCI runtime failure", (self.results / "fixture-server.log").read_text())

    def test_preflight_fails_after_the_server_started(self):
        summary, _ = self.run_failing_at("preflight")
        self.assertTrue(summary["reasons"][0].startswith("The fixture server failed its check"), summary)
        self.assertIn("serving on", (self.results / "fixture-server.log").read_text())


if __name__ == "__main__":
    unittest.main()
