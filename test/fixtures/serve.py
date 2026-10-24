#!/usr/bin/env python3
"""Serves files over HTTP with byte ranges, which httpfs uses to read Parquet.

Usage: serve.py --root DIR [--mount NAME=DIR] [--bind HOST] [--port N]
                [--port-file FILE] [--log FILE] [--json-log FILE]

test/smoke.sh serves its data directory and the committed fixtures with it,
and example/bench/run.sh serves public/ to the benchmark's Lambda containers.
A path under /NAME/ is served from the mount's DIR, any other path from
--root. GET and HEAD answer 200 with the whole file, or 206 with the requested
bytes for a Range of the form bytes=START- or bytes=START-END, 416 for a range
that starts past the end of the file, and 404 for a missing file. The server
speaks HTTP/1.0, so every request has a connection of its own.

--log appends a line "METHOD PATH AUTH" per request, where AUTH is the
Authorization scheme or "-", before the response is written: a client that has
its response finds the request logged, and one client's sequential requests in
order. It reopens the file for each line, so a caller may truncate it between
runs. --json-log writes one JSON object per request, "-" for standard output:
the method, path, query, Range header, status, the body bytes sent, the time
from having read the request's headers to having written its body, and as
"time" the wall-clock time the headers were read. It is written after the
response, so it adds nothing to the time the client waits for its bytes. A
client can therefore have its response before the record exists, and records
come in the order requests finished; sort them by "time" for the order they
started.

--port 0, the default, picks a free port. --port-file receives the port once
the server is listening, and a line "serving on HOST:PORT" goes to standard
error. Standard library only, so it runs on Amazon Linux 2023's Python 3.9.
"""
import argparse
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.parse


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True)
    parser.add_argument("--mount", action="append", default=[], metavar="NAME=DIR")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--port-file")
    parser.add_argument("--log")
    parser.add_argument("--json-log")
    args = parser.parse_args(argv)
    mounts = {}
    for mount in args.mount:
        name, sep, directory = mount.partition("=")
        if not sep or not name or "/" in name:
            parser.error(f"--mount {mount!r} isn't NAME=DIR")
        mounts[name] = directory
    args.mounts = mounts
    return args


def make_handler(root, mounts, log, json_log):
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def resolve(self, path):
            parts = [p for p in urllib.parse.unquote(path).split("/") if p]
            if ".." in parts:
                return None
            if parts and parts[0] in mounts:
                return os.path.join(mounts[parts[0]], *parts[1:])
            return os.path.join(root, *parts)

        def respond(self, send_body):
            started_at = time.time()
            started = time.monotonic()
            path, _, query = self.path.partition("?")
            byte_range = self.headers.get("Range")
            if log:
                auth = self.headers.get("Authorization", "").split(" ", 1)[0] or "-"
                with lock, open(log, "a") as f:
                    f.write(f"{self.command} {self.path} {auth}\n")
            status, sent, aborted = self.send_file(self.resolve(path), byte_range, send_body)
            duration_ms = (time.monotonic() - started) * 1000
            if json_log:
                with lock:
                    record = {
                        "time": started_at,
                        "client": self.client_address[0],
                        "method": self.command,
                        "path": path,
                        "query": query,
                        "range": byte_range,
                        "status": status,
                        "bytes": sent,
                        "duration_ms": round(duration_ms, 3),
                    }
                    if aborted:
                        record["aborted"] = True
                    json_log.write(json.dumps(record) + "\n")
                    json_log.flush()

        def send_file(self, path, byte_range, send_body):
            """Returns (status, body bytes sent, whether the client hung up)."""
            if not path or not os.path.isfile(path):
                self.send_error(404)
                return 404, 0, False
            size = os.path.getsize(path)
            start, end = 0, size - 1
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", byte_range or "")
            if match:
                start = int(match.group(1))
                end = min(int(match.group(2) or end), end)
                if start >= size or end < start:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return 416, 0, False
                status = 206
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                status = 200
                self.send_response(200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Last-Modified", self.date_time_string(int(os.path.getmtime(path))))
            self.end_headers()
            if not send_body:
                return status, 0, False
            with open(path, "rb") as f:
                f.seek(start)
                body = f.read(end - start + 1)
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return status, 0, True
            return status, len(body), False

        def do_HEAD(self):
            self.respond(False)

        def do_GET(self):
            self.respond(True)

        def log_message(self, format, *args):
            pass

    return Handler


def main(argv=None):
    args = parse_args(argv)
    json_log = None
    if args.json_log == "-":
        json_log = sys.stdout
    elif args.json_log:
        json_log = open(args.json_log, "a")
    handler = make_handler(args.root, args.mounts, args.log, json_log)
    server = http.server.ThreadingHTTPServer((args.bind, args.port), handler)
    host, port = server.server_address[:2]
    if args.port_file:
        with open(args.port_file + ".tmp", "w") as f:
            f.write(str(port))
        os.rename(args.port_file + ".tmp", args.port_file)
    print(f"serving on {host}:{port}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
