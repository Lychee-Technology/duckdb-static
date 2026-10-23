#!/usr/bin/env python3
"""Tests fixtures.py verify against a local server that serves test/fixtures/public
correctly, or with one of the faults verify must report, tests that generate
and check guard the committed versions, and tests that a DUCKDB_CLI override
still names the CLI after generation changes directories. The check test
downloads the pinned DuckDB CLI unless DUCKDB_CLI is set.

Usage: python3 test/fixtures/test_fixtures.py
"""
import gzip
import http.server
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures.py")
PUBLIC = os.path.join(HERE, "public")

sys.path.insert(0, HERE)
import fixtures


class Handler(http.server.BaseHTTPRequestHandler):
    # Set per server: "good", or the fault to serve.
    fault = "good"

    def respond(self, send_body):
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
