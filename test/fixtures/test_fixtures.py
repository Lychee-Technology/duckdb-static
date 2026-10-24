#!/usr/bin/env python3
"""Tests fixtures.py verify against a local server that serves test/fixtures/public
correctly, or with one of the faults verify must report, tests that generate
and check guard the committed versions, and tests that a DUCKDB_CLI override
still names the CLI after generation changes directories. Also tests serve.py,
the range server test/smoke.sh and example/bench/ use, and that verify passes
against it. The check test downloads the pinned DuckDB CLI unless DUCKDB_CLI
is set.

Usage: python3 test/fixtures/test_fixtures.py
"""
import gzip
import http.client
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures.py")
SERVE = os.path.join(HERE, "serve.py")
PUBLIC = os.path.join(HERE, "public")

sys.path.insert(0, HERE)
import fixtures


class Handler(http.server.BaseHTTPRequestHandler):
    # Set per server: "good", or the fault to serve.
    fault = "good"

    def respond(self, send_body):
        if self.fault == "rate_limited":
            return self.send(429, {}, b"", send_body)
        path = os.path.join(PUBLIC, self.path.lstrip("/"))
        if not os.path.isfile(path):
            if self.fault == "fallback_page":
                # Like a single-page-app host: 200 with a page for any path.
                return self.send(200, {}, b"<html></html>", send_body)
            return self.send(404, {}, b"", send_body)
        with open(path, "rb") as f:
            data = f.read()
        if self.fault == "stale" and path.endswith(".parquet"):
            data = data[:-5] + b"X" + data[-4:]
        headers = {"Accept-Ranges": "bytes"}
        if self.fault == "truncated" and path.endswith(".parquet"):
            # Declares the whole file, then closes after 5 bytes.
            return self.send(200, {**headers, "Content-Length": str(len(data))}, data[:5], send_body)
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
        if match and self.fault != "ignores_range":
            start, end = int(match.group(1)), int(match.group(2))
            headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
            return self.send(206, headers, data[start:end + 1], send_body)
        if self.fault == "compresses" and "gzip" in self.headers.get("Accept-Encoding", ""):
            headers["Content-Encoding"] = "gzip"
            data = gzip.compress(data, mtime=0)
        return self.send(200, headers, data, send_body)

    def send(self, status, headers, body, send_body):
        self.send_response(status)
        headers = {"Content-Length": str(len(body)), **headers}
        if self.fault == "no_content_length" and not send_body:
            # Like Cloudflare Pages: HEAD without a Content-Length.
            del headers["Content-Length"]
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def do_HEAD(self):
        self.respond(False)

    def do_GET(self):
        self.respond(True)

    def log_message(self, format, *args):
        pass


def run(script, *args):
    env = {k: v for k, v in os.environ.items() if k != "DUCKDB_TEST_HTTPFS_BASE_URL"}
    result = subprocess.run([sys.executable, script, *args],
                            env=env, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


def verify(*args):
    return run(FIXTURES, "verify", *args)


class VerifyTest(unittest.TestCase):
    def serve(self, fault):
        handler = type("FaultHandler", (Handler,), {"fault": fault})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def assert_fails(self, fault, message):
        code, output = verify(self.serve(fault))
        self.assertEqual(code, 1, output)
        self.assertIn(message, output)

    def test_correct_server_passes(self):
        code, output = verify(self.serve("good"))
        self.assertEqual(code, 0, output)
        self.assertNotIn("FAIL", output)
        # manifest, then HEAD, GET and three ranges per file, then the 404 check
        self.assertIn("22 of 22 checks passed", output)

    def test_ignored_range_fails(self):
        self.assert_fails("ignores_range", "status 200, expected 206: the server ignores Range")

    def test_rate_limit_fails(self):
        code, output = verify(self.serve("rate_limited"))
        self.assertEqual(code, 1, output)
        self.assertIn("FAIL manifest.json matches the committed copy: status 429, expected 200", output)
        # test/smoke.sh prints its rate-limited: line when a FAIL line matches this.
        self.assertRegex(output, re.compile(r"^FAIL .*: status 429,", re.MULTILINE))
        # A ranged read that got 429 says nothing about Range support.
        self.assertIn("FAIL GET basic.parquet bytes 0-3: status 429, expected 206\n", output)

    def test_compression_fails(self):
        self.assert_fails("compresses", "FAIL GET basic.parquet: Content-Encoding: gzip")

    def test_fallback_page_for_missing_file_fails(self):
        self.assert_fails("fallback_page", "HEAD does-not-exist.parquet is 404: status 200, expected 404")

    def test_stale_bytes_fail(self):
        self.assert_fails("stale", "differ from bytes")

    def test_missing_content_length_fails(self):
        self.assert_fails("no_content_length", "FAIL HEAD basic.parquet: Content-Length None, expected")

    def test_truncated_body_fails(self):
        code, output = verify(self.serve("truncated"))
        self.assertEqual(code, 1, output)
        self.assertIn("FAIL GET basic.parquet: protocol: IncompleteRead(5 bytes read", output)
        # The failure is one check, not the end of the run.
        self.assertIn("of 22 checks passed", output)

    def test_url_without_scheme_fails(self):
        code, output = verify("raw.githubusercontent.com/x/y")
        self.assertEqual(code, 4, output)
        self.assertIn("isn't an http:// or https:// URL", output)
        self.assertNotIn("Traceback", output)

    def test_malformed_url_fails(self):
        # urlsplit raises on the bracket and .port on the ports.
        for url in ("https://[::1", "http://127.0.0.1:abc", "http://127.0.0.1:99999"):
            code, output = verify(url)
            self.assertEqual(code, 4, output)
            self.assertIn("isn't an http:// or https:// URL", output)
            self.assertNotIn("Traceback", output)

    def test_unreachable_endpoint_fails(self):
        url = self.serve("good")
        # A port nothing listens on any more.
        self.doCleanups()
        code, output = verify(url)
        self.assertEqual(code, 1, output)
        self.assertIn("connection:", output)

    def test_unconfigured_endpoint_is_reported(self):
        code, output = verify()
        self.assertEqual(code, 2, output)
        self.assertIn("Not configured", output)


class ServeTest(unittest.TestCase):
    """serve.py, run as test/smoke.sh runs it: data/ as the root and the
    committed fixtures mounted at /public/."""

    def setUp(self):
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work)
        self.root = os.path.join(work, "data")
        os.mkdir(self.root)
        with open(os.path.join(self.root, "local.bin"), "wb") as f:
            f.write(bytes(range(100)))
        self.log = os.path.join(work, "requests.log")
        self.json_log = os.path.join(work, "requests.jsonl")
        port_file = os.path.join(work, "port")
        process = subprocess.Popen(
            [sys.executable, "-I", SERVE, "--root", self.root, "--mount", f"public={PUBLIC}",
             "--port-file", port_file, "--log", self.log, "--json-log", self.json_log],
            stderr=subprocess.PIPE, text=True)
        self.addCleanup(process.stderr.close)
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        for _ in range(100):
            if os.path.exists(port_file):
                break
            time.sleep(0.05)
        with open(port_file) as f:
            self.port = int(f.read())
        self.assertEqual(process.stderr.readline(), f"serving on 127.0.0.1:{self.port}\n")
        with open(os.path.join(PUBLIC, "v2", "tiny.parquet"), "rb") as f:
            self.tiny = f.read()

    def request(self, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def records(self, count):
        """Waits up to 10 s for count records in the JSON log, which the server
        writes after each response, and returns them in the order their
        requests started."""
        deadline = time.monotonic() + 10
        while True:
            with open(self.json_log) as f:
                records = [json.loads(line) for line in f if line.endswith("\n")]
            if len(records) >= count or time.monotonic() > deadline:
                return sorted(records, key=lambda r: r["time"])
            time.sleep(0.01)

    def test_head_and_get(self):
        status, headers, body = self.request("HEAD", "/public/v2/tiny.parquet")
        self.assertEqual((status, headers["Content-Length"], headers["Accept-Ranges"], body),
                         (200, str(len(self.tiny)), "bytes", b""))
        status, _, body = self.request("GET", "/public/v2/tiny.parquet")
        self.assertEqual((status, body), (200, self.tiny))

    def test_ranges(self):
        size = len(self.tiny)
        for header, start, end in (("bytes=0-3", 0, 3), (f"bytes={size - 8}-", size - 8, size - 1),
                                   (f"bytes=100-{size + 50}", 100, size - 1)):
            status, headers, body = self.request("GET", "/public/v2/tiny.parquet", {"Range": header})
            self.assertEqual(status, 206, header)
            self.assertEqual(headers["Content-Range"], f"bytes {start}-{end}/{size}")
            self.assertEqual(body, self.tiny[start:end + 1])

    def test_unsatisfiable_range_is_416(self):
        size = len(self.tiny)
        for header in (f"bytes={size}-", "bytes=10-5"):
            status, headers, body = self.request("GET", "/public/v2/tiny.parquet", {"Range": header})
            self.assertEqual((status, headers["Content-Range"], body), (416, f"bytes */{size}", b""), header)

    def test_root_missing_files_and_escapes(self):
        status, _, body = self.request("GET", "/local.bin")
        self.assertEqual((status, body), (200, bytes(range(100))))
        for path in ("/missing.parquet", "/public/v2/missing.parquet", "/public/../data/local.bin",
                     "/public/%2e%2e/v1/basic.parquet"):
            self.assertEqual(self.request("GET", path)[0], 404, path)

    def test_logs(self):
        lines = []
        for method, path, headers, auth in (
                ("HEAD", "/public/v2/tiny.parquet?instance=abc", {"Authorization": "Bearer x"}, "Bearer"),
                ("GET", "/public/v2/tiny.parquet?instance=abc", {"Range": "bytes=0-9"}, "-"),
                ("GET", "/missing.parquet", {}, "-")):
            self.request(method, path, headers)
            # test/smoke.sh reads this log as soon as its client exits, so each
            # request must be in it by the time its response has arrived.
            lines.append(f"{method} {path} {auth}\n")
            with open(self.log) as f:
                self.assertEqual(f.read(), "".join(lines))
        records = self.records(3)
        for record in records:
            self.assertGreaterEqual(record.pop("duration_ms"), 0)
            self.assertEqual(record.pop("client"), "127.0.0.1")
            self.assertIsInstance(record.pop("time"), float)
        self.assertEqual(records, [
            {"method": "HEAD", "path": "/public/v2/tiny.parquet", "query": "instance=abc", "range": None,
             "status": 200, "bytes": 0},
            {"method": "GET", "path": "/public/v2/tiny.parquet", "query": "instance=abc", "range": "bytes=0-9",
             "status": 206, "bytes": 10},
            {"method": "GET", "path": "/missing.parquet", "query": "", "range": None, "status": 404, "bytes": 0},
        ])

    def test_verify_passes(self):
        # As example/bench/run.sh checks the server before any sample.
        for version, checks in (("v1", 22), ("v2", 7)):
            code, output = verify(f"http://127.0.0.1:{self.port}/public", "--version", version)
            self.assertEqual(code, 0, output)
            self.assertIn(f"{checks} of {checks} checks passed", output)


class CliOverrideTest(unittest.TestCase):
    """DUCKDB_CLI must still name the CLI after build changes directories."""

    def setUp(self):
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work)
        self.bin = os.path.join(work, "bin")
        os.mkdir(self.bin)
        # Stands in for the CLI: reports v2.0.0 and its working directory.
        with open(os.path.join(self.bin, "duckdb"), "w") as f:
            f.write("#!/bin/sh\ncat >/dev/null\necho \"v2.0.0 $(pwd -P)\"\n")
        os.chmod(os.path.join(self.bin, "duckdb"), 0o755)
        self.elsewhere = os.path.realpath(os.path.join(work, "out"))
        os.mkdir(self.elsewhere)
        cwd = os.getcwd()
        os.chdir(self.bin)
        self.addCleanup(os.chdir, cwd)

    def cli(self, value, path=None):
        env = {"DUCKDB_CLI": value}
        if path is not None:
            env["PATH"] = path
        with mock.patch.dict(os.environ, env):
            return fixtures.cli_path("v2.0.0", self.elsewhere)

    def test_relative_path_runs_from_another_directory(self):
        cli = self.cli("./duckdb")
        self.assertEqual(fixtures.run_cli(cli, "", self.elsewhere).split()[1], self.elsewhere)

    def test_name_is_looked_up_in_path(self):
        cli = self.cli("duckdb", self.bin + os.pathsep + os.environ["PATH"])
        self.assertEqual(fixtures.run_cli(cli, "", self.elsewhere).split()[1], self.elsewhere)

    def test_missing_cli_fails(self):
        with self.assertRaisesRegex(fixtures.Failure, "isn't an executable file"):
            self.cli("./missing")


class GenerateCheckTest(unittest.TestCase):
    def test_generate_refuses_existing_version(self):
        code, output = run(FIXTURES, "generate", "v1")
        self.assertEqual(code, 1, output)
        self.assertIn("Published fixtures don't change", output)

    def test_check_reports_changed_bytes(self):
        # check on a copy of the tree with one byte of basic.parquet changed.
        with tempfile.TemporaryDirectory() as work:
            for name in ("fixtures.py", "v1.sql", "v1.queries.sql"):
                shutil.copy(os.path.join(HERE, name), work)
            shutil.copytree(PUBLIC, os.path.join(work, "public"))
            path = os.path.join(work, "public", "v1", "basic.parquet")
            with open(path, "rb") as f:
                data = bytearray(f.read())
            offset = len(data) // 2
            data[offset] ^= 0xFF
            with open(path, "wb") as f:
                f.write(data)
            code, output = run(os.path.join(work, "fixtures.py"), "check", "v1")
        self.assertEqual(code, 1, output)
        self.assertIn(f"basic.parquet: {len(data)} bytes committed, {len(data)} regenerated, "
                      f"first difference at byte {offset}", output)
        self.assertIn("regenerated files don't match", output)


if __name__ == "__main__":
    unittest.main()
