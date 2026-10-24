#!/usr/bin/env python3
"""Generates, checks and verifies the Parquet fixtures in test/fixtures/public.

  fixtures.py generate <version>    write public/<version>/ from <version>.sql
  fixtures.py check [<version>]     regenerate into a temporary directory and
                                    compare byte for byte with public/<version>/,
                                    or with every version in public/ if none
                                    is given
  fixtures.py verify [<base URL>] [--version <version>]
                                    check that <base URL>/<version>/ serves the
                                    committed files with working byte ranges

A fixture version is immutable once committed: generate refuses to overwrite
it, and a change of data or of the pinned DuckDB means a new version. The
files are written by the official DuckDB CLI pinned in VERSIONS, which is
downloaded and checked against its sha256. DUCKDB_CLI=<path> uses a local copy
instead, which must report the same version.

verify reads the base URL from DUCKDB_TEST_HTTPFS_BASE_URL when none is given.
It exits 0 when every check passed, 1 when one failed, 2 when no endpoint is
configured and 4 when the base URL isn't an http:// or https:// URL. Only the Python standard library is used, so it runs on Amazon
Linux 2023's Python 3.9.
"""
import argparse
import hashlib
import http.client
import io
import json
import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(HERE, "public")

# The DuckDB CLI that writes each fixture version, and the sha256 of its
# release tarball per machine type.
VERSIONS = {"v1": "v2.0.0", "v2": "v2.0.0"}
CLI_RELEASES = {
    "v2.0.0": {
        "x86_64": ("duckdb-cli-linux-amd64.tar.gz",
                   "c6507206baf7793c6013dbd82368469a45f20dc0e17a6f92f204602f652ed2bc"),
        "aarch64": ("duckdb-cli-linux-arm64.tar.gz",
                    "859e1875d2a9515fbb4bf8871061437a14af82320da68c753f094c4b776348fe"),
    },
}

# A path the endpoint must answer with 404, so a missing-file test sees an
# error instead of a fallback page.
ABSENT = "does-not-exist.parquet"


class Failure(Exception):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    with open(path, "rb") as f:
        return f.read()


def parse_queries(path):
    """Returns [{"sql", "expected"}] from a <version>.queries.sql file."""
    queries, entry = [], None
    with open(path, encoding="utf-8") as f:
        for number, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if entry is None:
                if line.startswith("-- expect: "):
                    entry = {"expected": line[len("-- expect: "):], "lines": [], "line": number}
                elif line and not line.startswith("--"):
                    raise Failure(f"{path}:{number}: query without a '-- expect: ' line")
                continue
            entry["lines"].append(line)
            if line.endswith(";"):
                sql = "\n".join(entry["lines"])[:-1]
                queries.append({"sql": sql, "expected": entry["expected"]})
                entry = None
    if entry is not None:
        raise Failure(f"{path}:{entry['line']}: query doesn't end with ';'")
    if not queries:
        raise Failure(f"{path}: no queries")
    return queries


def cli_path(duckdb_version, work):
    """Returns a DuckDB CLI of duckdb_version, downloading it if needed."""
    path = os.environ.get("DUCKDB_CLI")
    if path:
        # build runs the CLI from its output directory, so a relative path is
        # made absolute here. A name without a slash is looked up in PATH.
        found = shutil.which(path)
        if not found:
            raise Failure(f"DUCKDB_CLI={path} isn't an executable file")
        path = os.path.abspath(found)
    else:
        machine = platform.machine()
        if machine not in CLI_RELEASES[duckdb_version]:
            raise Failure(f"no pinned DuckDB {duckdb_version} CLI for {machine}")
        name, digest = CLI_RELEASES[duckdb_version][machine]
        url = f"https://github.com/duckdb/duckdb/releases/download/{duckdb_version}/{name}"
        print(f"Downloading {url}")
        with urllib.request.urlopen(url, timeout=120) as response:
            tarball = response.read()
        if sha256(tarball) != digest:
            raise Failure(f"{name} has sha256 {sha256(tarball)}, expected {digest}")
        with tarfile.open(fileobj=io.BytesIO(tarball)) as tar:
            member = tar.extractfile("duckdb")
            path = os.path.join(work, "duckdb")
            with open(path, "wb") as f:
                shutil.copyfileobj(member, f)
        os.chmod(path, 0o755)
    reported = run_cli(path, ".version", os.getcwd())
    if f" {duckdb_version} " not in f" {reported} ":
        raise Failure(f"{path} reports {reported.strip()!r}, expected DuckDB {duckdb_version}")
    return path


def run_cli(cli, sql, cwd, *flags):
    """Runs sql in an in-memory database and returns stdout.

    -no-agent and -init /dev/null keep the environment (AI_AGENT, ~/.duckdbrc)
    from changing the output. Extension autoloading is off, so nothing is
    downloaded and the result can't depend on the network.
    """
    env = {k: v for k, v in os.environ.items() if k != "AI_AGENT"}
    script = ("SET autoinstall_known_extensions = false;\n"
              "SET autoload_known_extensions = false;\n"
              "SET threads = 1;\n" + sql + "\n")
    result = subprocess.run([cli, "-no-agent", "-init", "/dev/null", "-bail", *flags],
                            input=script, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise Failure(f"DuckDB failed:\n{result.stderr.strip()}\nSQL:\n{sql}")
    return result.stdout


def run_json(cli, sql, cwd):
    out = run_cli(cli, sql, cwd, "-json").strip()
    return json.loads(out) if out else []


def check_queries(cli, queries, directory):
    """Runs every query against the files in directory and compares results."""
    failures = 0
    for query in queries:
        sql = query["sql"].replace("{dir}", directory)
        rows = run_json(cli, f"SELECT CAST(COLUMNS(*) AS VARCHAR) FROM ({sql})", directory)
        values = [list(row.values()) for row in rows]
        if len(values) != 1 or len(values[0]) != 1:
            got = f"{len(values)} row(s) of {len(values[0]) if values else 0} column(s)"
        elif values[0][0] != query["expected"]:
            got = repr(values[0][0])
        else:
            print(f"ok   {query['sql']} -> {query['expected']}")
            continue
        print(f"FAIL {query['sql']}\n     expected {query['expected']!r}, got {got}")
        failures += 1
    if failures:
        raise Failure(f"{failures} of {len(queries)} queries didn't give the expected result")


def duckdb_version_of(version):
    if version not in VERSIONS:
        raise Failure(f"unknown fixture version {version}; known: {', '.join(VERSIONS)}")
    return VERSIONS[version]


def build(version, cli, out):
    """Writes the files and manifest of version into the empty directory out."""
    duckdb_version = duckdb_version_of(version)
    queries = parse_queries(os.path.join(HERE, f"{version}.queries.sql"))
    with open(os.path.join(HERE, f"{version}.sql"), encoding="utf-8") as f:
        run_cli(cli, f.read(), out)
    check_queries(cli, queries, out)

    files = []
    for name in sorted(os.listdir(out)):
        data = read(os.path.join(out, name))
        meta = run_json(cli, f"SELECT num_rows, num_row_groups FROM parquet_file_metadata('{name}')", out)[0]
        schema = run_json(cli, f"DESCRIBE SELECT * FROM read_parquet('{name}')", out)
        files.append({
            "path": name,
            "size": len(data),
            "sha256": sha256(data),
            "rows": meta["num_rows"],
            "row_groups": meta["num_row_groups"],
            "schema": [{"name": c["column_name"], "type": c["column_type"]} for c in schema],
        })
    manifest = {
        "version": version,
        "generator": f"test/fixtures/fixtures.py with the DuckDB {duckdb_version} CLI",
        "absent": ABSENT,
        "total_size": sum(f["size"] for f in files),
        "files": files,
        "queries": queries,
    }
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")


def generate(version):
    target = os.path.join(PUBLIC, version)
    if os.path.exists(target):
        raise Failure(f"{target} exists. Published fixtures don't change: run check, "
                      "or add a new version for new data.")
    with tempfile.TemporaryDirectory() as work:
        out = os.path.join(work, version)
        os.mkdir(out)
        build(version, cli_path(duckdb_version_of(version), work), out)
        shutil.copytree(out, target)
    print(f"Wrote {target}")


def describe_difference(cli, committed, regenerated):
    """Prints where two versions of a file first differ, and for Parquet files
    which column chunks differ, to show what the generator changed."""
    a, b = read(committed), read(regenerated)
    offset = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    print(f"     {os.path.basename(committed)}: {len(a)} bytes committed, {len(b)} regenerated, "
          f"first difference at byte {offset}")
    if not committed.endswith(".parquet"):
        return
    sql = ("SELECT * EXCLUDE (file_name) FROM parquet_metadata('{}') ORDER BY row_group_id, column_id")
    chunks_a = run_json(cli, sql.format(committed), os.path.dirname(committed))
    chunks_b = run_json(cli, sql.format(regenerated), os.path.dirname(regenerated))
    shown = 0
    for chunk_a, chunk_b in zip(chunks_a, chunks_b):
        changed = {k: (chunk_a[k], chunk_b.get(k)) for k in chunk_a if chunk_a[k] != chunk_b.get(k)}
        if changed and shown < 3:
            shown += 1
            print(f"     row group {chunk_a['row_group_id']}, column {chunk_a['path_in_schema']}: " +
                  ", ".join(f"{k} {old!r} -> {new!r}" for k, (old, new) in changed.items()))


def check(version):
    target = os.path.join(PUBLIC, version)
    with tempfile.TemporaryDirectory() as work:
        out = os.path.join(work, version)
        os.mkdir(out)
        cli = cli_path(duckdb_version_of(version), work)
        build(version, cli, out)
        expected, actual = sorted(os.listdir(out)), sorted(os.listdir(target))
        if expected != actual:
            raise Failure(f"{target} holds {actual}, the generator writes {expected}")
        differ = [n for n in expected if read(os.path.join(out, n)) != read(os.path.join(target, n))]
        for name in differ:
            describe_difference(cli, os.path.join(target, name), os.path.join(out, name))
        if differ:
            raise Failure(f"regenerated files don't match {target}: {', '.join(differ)}")
    print(f"{target} matches a fresh generation")


def classify(error):
    """Names the layer an HTTP request failed in, for the verify report."""
    reason = getattr(error, "reason", error)
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(reason, socket.gaierror):
        return f"DNS: {reason}"
    if isinstance(reason, ssl.SSLError):
        return f"TLS: {reason}"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return f"timeout: {reason}"
    if isinstance(reason, http.client.HTTPException):
        # A malformed or truncated response, such as a body shorter than its
        # Content-Length.
        return f"protocol: {reason}"
    return f"connection: {reason}"


def fetch(url, method="GET", byte_range=None, offer_compression=True):
    """Returns (status, headers, body) without decoding the body.

    offer_compression asks for gzip or brotli, so a server that would compress
    a Parquet file for some client does it here, and the check sees it.
    """
    headers = {"User-Agent": "duckdb-static-fixtures-verify"}
    if offer_compression:
        headers["Accept-Encoding"] = "gzip, br"
    if byte_range:
        headers["Range"] = "bytes=%d-%d" % byte_range
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, b""


def verify(base, version):
    if not base:
        print("Not configured: pass a base URL or set DUCKDB_TEST_HTTPFS_BASE_URL. "
              "Nothing was verified.")
        return 2
    try:
        parsed = urllib.parse.urlsplit(base)
        parsed.port  # raises on a port that isn't a number from 0 to 65535
    except ValueError:  # also an unclosed IPv6 bracket
        parsed = None
    if not parsed or parsed.scheme not in ("http", "https") or not parsed.netloc:
        # A mistyped base URL is a failure, not "not configured": exit 2 would
        # let a CI job read it as a skip. Nor is it a failed check of the host,
        # so a caller can tell a typo from a host that serves the files wrong.
        print(f"FAIL {base!r} isn't an http:// or https:// URL. Nothing was verified.")
        return 4
    local = os.path.join(PUBLIC, version)
    manifest_bytes = read(os.path.join(local, "manifest.json"))
    manifest = json.loads(manifest_bytes)
    directory = f"{base.rstrip('/')}/{version}"
    print(f"Verifying {directory}/ against {local}")

    results = []

    def check_one(name, test):
        try:
            problem = test()
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            problem = classify(e)
        results.append(problem is None)
        print(f"ok   {name}" if problem is None else f"FAIL {name}: {problem}")

    def same_bytes(url, data, offer_compression=True):
        def test():
            status, headers, body = fetch(url, offer_compression=offer_compression)
            if status != 200:
                return f"status {status}, expected 200"
            if headers.get("Content-Encoding"):
                return f"Content-Encoding: {headers['Content-Encoding']}"
            if body != data:
                return f"{len(body)} bytes with sha256 {sha256(body)}, expected {len(data)} with {sha256(data)}"
            return None
        return test

    def head(url, size):
        def test():
            status, headers, _ = fetch(url, "HEAD")
            if status != 200:
                return f"status {status}, expected 200"
            if headers.get("Content-Encoding"):
                return f"Content-Encoding: {headers['Content-Encoding']}"
            if headers.get("Content-Length") != str(size):
                return f"Content-Length {headers.get('Content-Length')}, expected {size}"
            if headers.get("Accept-Ranges") != "bytes":
                print(f"warn {url}: Accept-Ranges is {headers.get('Accept-Ranges')!r}, not 'bytes'")
            return None
        return test

    def ranged(url, data, start, end):
        def test():
            status, headers, body = fetch(url, byte_range=(start, end))
            if status == 200:
                return f"status {status}, expected 206: the server ignores Range"
            if status != 206:
                return f"status {status}, expected 206"
            if headers.get("Content-Encoding"):
                return f"Content-Encoding: {headers['Content-Encoding']}"
            content_range = f"bytes {start}-{end}/{len(data)}"
            if headers.get("Content-Range") != content_range:
                return f"Content-Range {headers.get('Content-Range')!r}, expected {content_range!r}"
            if body != data[start:end + 1]:
                return f"{len(body)} bytes that differ from bytes {start}-{end} of the file"
            return None
        return test

    def absent(url):
        def test():
            status, _, _ = fetch(url, "HEAD")
            return None if status == 404 else f"status {status}, expected 404"
        return test

    # httpfs never reads the manifest, so this request doesn't offer
    # compression: a host that compresses JSON for clients that ask still
    # passes. One that compresses it unasked fails, since its bytes differ.
    check_one("manifest.json matches the committed copy",
              same_bytes(f"{directory}/manifest.json", manifest_bytes, offer_compression=False))
    for entry in manifest["files"]:
        data = read(os.path.join(local, entry["path"]))
        if sha256(data) != entry["sha256"]:
            raise Failure(f"{local}/{entry['path']} doesn't match its manifest sha256")
        url = f"{directory}/{entry['path']}"
        size = len(data)
        check_one(f"HEAD {entry['path']}", head(url, size))
        check_one(f"GET {entry['path']}", same_bytes(url, data))
        # httpfs reads the footer first, then the column chunks it needs.
        for start, end in ((0, 3), (size - 8, size - 1), (size // 2, min(size // 2 + 4095, size - 1))):
            check_one(f"GET {entry['path']} bytes {start}-{end}", ranged(url, data, start, end))
    check_one(f"HEAD {manifest['absent']} is 404", absent(f"{directory}/{manifest['absent']}"))

    failed = results.count(False)
    print(f"{len(results) - failed} of {len(results)} checks passed")
    return 1 if failed or not results else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("generate").add_argument("version")
    commands.add_parser("check").add_argument("version", nargs="?")
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("base", nargs="?", default=os.environ.get("DUCKDB_TEST_HTTPFS_BASE_URL", ""))
    verify_parser.add_argument("--version", default="v1")
    args = parser.parse_args()
    try:
        if args.command == "generate":
            generate(args.version)
        elif args.command == "check":
            # Every committed version, so CI keeps covering new ones; one
            # without a VERSIONS entry fails.
            for version in [args.version] if args.version else sorted(os.listdir(PUBLIC)):
                check(version)
        else:
            return verify(args.base, args.version)
    except Failure as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
