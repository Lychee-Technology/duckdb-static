#!/usr/bin/env python3
"""Tests fixtures.py verify against a local server that serves test/fixtures/public
correctly, or with one of the faults verify must report.

Usage: python3 test/fixtures/test_fixtures.py
"""
import gzip
import http.server
import os
import re
import subprocess
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures.py")
PUBLIC = os.path.join(HERE, "public")


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
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def do_HEAD(self):
        self.respond(False)

    def do_GET(self):
        self.respond(True)

    def log_message(self, format, *args):
        pass


def verify(*args):
    env = {k: v for k, v in os.environ.items() if k != "DUCKDB_TEST_HTTPFS_BASE_URL"}
    result = subprocess.run([sys.executable, FIXTURES, "verify", *args],
                            env=env, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


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


if __name__ == "__main__":
    unittest.main()
