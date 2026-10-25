#!/usr/bin/env python3
"""Tests summarize.py's status decisions and statistics on synthetic results.

Usage: python3 example/bench/test_summarize.py [-v]
"""

import contextlib
import copy
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import summarize  # noqa: E402

MANIFEST = Path(HERE, "..", "..", "test", "fixtures", "public", "v2", "manifest.json")
FIXTURE = next(f for f in json.loads(MANIFEST.read_text())["files"] if f["path"] == "tiny.parquet")
URL = "http://fixtures:8080/v2/tiny.parquet"
EXT_DIR = "/var/task/ext"
STOCK_HTTPFS = f"{EXT_DIR}/v1.5.6/linux_amd64/httpfs.duckdb_extension"
# What each variant reports as its source_id for DuckDB v1.5.6.
SOURCE_IDS = {"stock": "069cc9f9b5", "custom": "069cc9f"}


def response(variant, n):
    instance = f"{variant}-{n}"
    extensions = []
    for name in summarize.EXTENSIONS:
        state = {"name": name, "loaded": True, "installed": True,
                 "install_mode": "STATICALLY_LINKED", "install_path": "(BUILT-IN)"}
        if variant == "stock" and name == "httpfs":
            state.update(install_mode="UNKNOWN", install_path=STOCK_HTTPFS)
        extensions.append(state)
    return {
        "mode": "bench",
        "variant": variant,
        "instance_id": instance,
        "invocation": 1,
        "pid": 1,
        "bootstrap_to_main_ms": 40.0 + n,
        "duckdb_open_or_connect_ms": 20.0 + n,
        "extension_ready_ms": 1.0,
        "first_http_parquet_query_ms": 2.0,
        "duckdb_ready_and_first_http_query_ms": 23.0 + n,
        "url": f"{URL}?instance={instance}",
        "result": 720600,
        "result_ok": True,
        "error": "",
        "library_version": "v1.5.6",
        "source_id": SOURCE_IDS[variant],
        "platform": "linux_amd64",
        "extensions": extensions,
        "settings": {
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
            "extension_directory": EXT_DIR,
        },
        "executable_bytes": 1,
    }


def requests_of(instance):
    """What the fixture server logs for one read of tiny.parquet."""
    query = f"instance={instance}"
    size = FIXTURE["size"]
    return [
        {"method": "HEAD", "path": "/v2/tiny.parquet", "query": query, "range": None,
         "status": 200, "bytes": 0, "duration_ms": 0.1},
        {"method": "GET", "path": "/v2/tiny.parquet", "query": query, "range": f"bytes=0-{size - 1}",
         "status": 206, "bytes": size, "duration_ms": 0.4},
    ]


class SummarizeTest(unittest.TestCase):
    SAMPLES = 4

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.prov = {
            "arch": "amd64",
            "duckdb_version": "v1.5.6",
            "samples_requested": self.SAMPLES,
            "fixture": {"url": URL, "path": "v2/tiny.parquet", "bytes": FIXTURE["size"], "sha256": FIXTURE["sha256"]},
        }
        self.warmup = [self.record("stock", 0, 1000), self.record("custom", 0, 1000)]
        self.samples = [
            self.record(v, i, i) for i in range(1, self.SAMPLES + 1) for v in summarize.VARIANTS
        ]
        self.remote = []
        self.requests = [q for r in self.warmup + self.samples for q in requests_of(r["response"]["instance_id"])]

    def record(self, variant, iteration, n):
        return {
            "variant": variant,
            "iteration": iteration,
            "position": 1,
            "invoke_status": 0,
            "response": response(variant, n),
        }

    def run_summary(self):
        for name, rows in (("warmup", self.warmup), ("samples", self.samples), ("remote", self.remote),
                           ("fixture-requests", self.requests)):
            (self.dir / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        (self.dir / "provenance.json").write_text(json.dumps(self.prov))
        manifest = json.loads(MANIFEST.read_text())
        prov, valid, summary = summarize.summarize(self.dir, manifest)
        summarize.markdown(prov, valid, summary)  # must not raise
        return summary

    def assert_status(self, status, reason=None):
        summary = self.run_summary()
        self.assertEqual(summary["status"], status, summary["reasons"])
        if reason:
            self.assertIn(reason, " ".join(summary["reasons"]))
        return summary

    def drop_requests(self, instance):
        self.requests = [q for q in self.requests if q["query"] != f"instance={instance}"]

    def test_expected_sum_matches_the_handler(self):
        # main.go checks the result itself, against its own constant.
        main = Path(HERE, "main.go").read_text()
        constant = int(re.search(r"^\s*expectedSum\s*=\s*(\d+)", main, re.MULTILINE).group(1))
        self.assertEqual(summarize.expected_sum(json.loads(MANIFEST.read_text())), constant)
        self.assertEqual(constant, 720600)

    def test_valid(self):
        summary = self.assert_status("valid")
        self.assertEqual(summary["valid_samples"], {"stock": 4, "custom": 4})
        total = summary["metrics"]["duckdb_ready_and_first_http_query_ms"]["stock"]
        # 24, 25, 26, 27: inclusive quantiles put q1 at 24.75, q3 at 26.25 and
        # p95 at 26.85.
        self.assertEqual((total["n"], total["median"], total["min"], total["max"]), (4, 25.5, 24.0, 27.0))
        self.assertAlmostEqual(total["iqr"], 1.5)
        self.assertAlmostEqual(total["p95"], 26.85)
        self.assertEqual(set(summary["metrics"]), {key for key, _ in summarize.METRICS})

    def test_stats(self):
        s = summarize.stats([float(v) for v in range(1, 21)])
        self.assertEqual((s["n"], s["median"], s["min"], s["max"]), (20, 10.5, 1.0, 20.0))
        # Inclusive quantiles of 1..20: q1 5.75, q3 15.25, p95 19.05.
        self.assertAlmostEqual(s["iqr"], 9.5)
        self.assertAlmostEqual(s["p95"], 19.05)
        one = summarize.stats([7.0])
        self.assertEqual((one["n"], one["median"], one["p95"], one["iqr"]), (1, 7.0, 7.0, 0.0))

    def test_http_requests_are_summarized(self):
        summary = self.assert_status("valid")
        for v in summarize.VARIANTS:
            http = summary["http"][v]
            self.assertEqual((http["requests"]["median"], http["bytes"]["median"]), (2, FIXTURE["size"]))
            self.assertAlmostEqual(http["server_ms"]["median"], 0.5)
            self.assertEqual(http["sequences"], [{
                "requests": ["HEAD - 200 0", f"GET bytes=0-{FIXTURE['size'] - 1} 206 {FIXTURE['size']}"],
                "samples": 4,
            }])

    def test_http_sequence_is_in_start_order(self):
        # The server logs each request after answering it, so the GET's record
        # can come first; its "time" says when the request started.
        for i, q in enumerate(self.requests):
            q["time"] = 1000.0 + i
        for i in range(0, len(self.requests), 2):
            self.requests[i], self.requests[i + 1] = self.requests[i + 1], self.requests[i]
        summary = self.assert_status("valid")
        for v in summarize.VARIANTS:
            self.assertEqual(summary["http"][v]["sequences"], [{
                "requests": ["HEAD - 200 0", f"GET bytes=0-{FIXTURE['size'] - 1} 206 {FIXTURE['size']}"],
                "samples": 4,
            }])

    def test_other_lines_and_requests_are_ignored(self):
        # fixtures.py verify's requests carry no instance ID, and a container
        # runtime may add lines of its own to the log.
        self.requests.append({"method": "GET", "path": "/v2/manifest.json", "query": "", "range": None,
                              "status": 200, "bytes": 300, "duration_ms": 0.1})
        self.run_summary()
        path = self.dir / "fixture-requests.jsonl"
        path.write_text("Emulate Docker CLI using podman.\n" + path.read_text())
        self.assertEqual(len(summarize.load_requests(path)), len(self.warmup + self.samples))
        summary = summarize.summarize(self.dir, json.loads(MANIFEST.read_text()))[2]
        self.assertEqual(summary["status"], "valid", summary["reasons"])

    def test_no_faster_variant_is_still_valid(self):
        for r in self.samples:
            r["response"]["duckdb_ready_and_first_http_query_ms"] = 99.0 if r["variant"] == "custom" else 1.0
        self.assert_status("valid")

    def test_sample_without_logged_requests_is_rejected(self):
        self.drop_requests(self.samples[0]["response"]["instance_id"])
        self.assert_status("invalid samples", "the fixture server logged no request from its process")

    def test_error_response_from_the_server_is_rejected(self):
        instance = self.samples[1]["response"]["instance_id"]
        self.drop_requests(instance)
        self.requests += [
            {"method": "GET", "path": "/v2/tiny.parquet", "query": f"instance={instance}",
             "range": "bytes=99999-", "status": 416, "bytes": 0, "duration_ms": 0.1},
        ] + requests_of(instance)
        self.assert_status("invalid samples", "answered GET bytes=99999- with 416")

    def test_sample_that_read_another_file_is_rejected(self):
        instance = self.samples[1]["response"]["instance_id"]
        for q in self.requests:
            if q["query"] == f"instance={instance}":
                q["path"] = "/v1/basic.parquet"
        self.assert_status("invalid samples", "requested /v1/basic.parquet")

    def test_sample_without_fixture_bytes_is_rejected(self):
        instance = self.samples[1]["response"]["instance_id"]
        self.requests = [q for q in self.requests if not (q["query"] == f"instance={instance}" and q["method"] == "GET")]
        self.assert_status("invalid samples", "sent its process no fixture bytes")

    def test_reused_process_is_rejected(self):
        self.samples[3]["response"]["instance_id"] = self.samples[1]["response"]["instance_id"]
        self.assert_status("invalid samples", "used before")

    def test_warmup_process_reused_is_rejected(self):
        self.samples[0]["response"]["instance_id"] = self.warmup[0]["response"]["instance_id"]
        self.assert_status("invalid samples")

    def test_second_invocation_is_rejected(self):
        self.samples[2]["response"]["invocation"] = 2
        self.assert_status("invalid samples")

    def test_missing_metric_is_rejected(self):
        del self.samples[0]["response"]["bootstrap_to_main_ms"]
        self.assert_status("invalid samples", "bootstrap_to_main_ms")

    def test_invalid_metrics_are_rejected(self):
        for value in (None, -1.0, float("nan"), "3", True):
            with self.subTest(value=value):
                self.setUp()
                self.samples[0]["response"]["first_http_parquet_query_ms"] = value
                self.assert_status("invalid samples", "first_http_parquet_query_ms is missing or invalid")

    def test_no_response_is_rejected(self):
        self.samples[0]["response"] = None
        self.samples[0]["invoke_status"] = 1
        self.assert_status("invalid samples", "sam local invoke exited 1")

    def test_every_sample_of_a_variant_rejected_keeps_the_reasons(self):
        for r in self.samples:
            if r["variant"] == "custom":
                r["response"], r["invoke_status"] = None, 1
        summary = self.assert_status("invalid samples", "custom iteration 1: no response (sam local invoke exited 1)")
        self.assertEqual(summary["valid_samples"], {"stock": 4, "custom": 0})
        self.assertIn("custom: 0 valid samples of 4 requested.", summary["reasons"])

    def test_every_sample_rejected_keeps_the_reasons(self):
        for r in self.samples:
            r["response"], r["invoke_status"] = None, 1
        summary = self.assert_status("invalid samples", "stock iteration 1: no response (sam local invoke exited 1)")
        self.assertEqual(summary["valid_samples"], {"stock": 0, "custom": 0})

    def test_wrong_result_is_a_functional_failure(self):
        self.samples[0]["response"]["result_ok"] = False
        self.samples[0]["response"]["result"] = 1
        self.assert_status("functional failure", "sum(id) was 1, not 720600")

    def test_result_is_checked_against_the_manifest(self):
        # The handler's own check passed, but its constant was wrong.
        self.samples[0]["response"]["result"] = 1249975000
        self.assert_status("functional failure", "not 720600")

    def test_function_error_is_a_functional_failure(self):
        self.samples[0]["response"] = {"errorType": "Runtime.ExitError", "errorMessage": "exit status 2"}
        self.assert_status("functional failure")

    def test_unreachable_fixture_server_is_not_run(self):
        # run.sh stops before any sample when the server fails its check.
        self.prov["setup"] = {"status": "not run", "reason": "The fixture server failed its check"}
        self.samples, self.requests = [], []
        summary = self.assert_status("not run")
        self.assertEqual(summary["reasons"], ["The fixture server failed its check"])

    def test_fixture_server_failing_during_the_samples_wins(self):
        self.prov["setup"] = {"status": "invalid samples", "reason": "The fixture server failed its check after"}
        self.samples[0]["response"]["error"] = "IO Error: Could not establish connection"
        self.assert_status("invalid samples", "after")

    def test_different_duckdb_builds_are_incomparable(self):
        for r in self.samples:
            if r["variant"] == "stock":
                r["response"]["source_id"] = "0000000000"
        self.assert_status("incomparable", "different DuckDB builds")

    def test_abbreviations_of_one_commit_are_one_build(self):
        # The default responses already differ in length; a third length of
        # the same commit is still the same build.
        self.samples[0]["response"]["source_id"] = "069cc9f9b5be"
        self.assert_status("valid")

    def test_missing_or_short_source_id_is_incomparable(self):
        for value in ("", None, "069cc9"):
            with self.subTest(value=value):
                self.setUp()
                for r in self.samples:
                    if r["variant"] == "custom":
                        r["response"]["source_id"] = value
                self.assert_status("incomparable", "different DuckDB builds")

    def test_another_duckdb_version_is_incomparable(self):
        # Both variants agree, but neither is the DuckDB this branch builds.
        for r in self.samples:
            r["response"]["library_version"] = "v1.5.5"
        self.assert_status("incomparable", "The samples report DuckDB v1.5.5, but duckdb_version is v1.5.6.")

    def test_other_loaded_extensions_are_listed(self):
        # duckdb-go-bindings' library links autocomplete, which DuckDB 1.5.6
        # loads on open. It is reported, and changes no status.
        for r in self.samples:
            if r["variant"] == "stock":
                r["response"]["extensions"].append({"name": "autocomplete", "loaded": True, "installed": True,
                                                    "install_mode": "STATICALLY_LINKED", "install_path": ""})
        self.assert_status("valid")
        prov, valid, summary = summarize.summarize(self.dir, json.loads(MANIFEST.read_text()))
        self.assertIn("| autocomplete | STATICALLY_LINKED | - |", summarize.markdown(prov, valid, summary))

    def test_unloaded_extension_is_incomparable(self):
        self.samples[1]["response"]["extensions"][1]["loaded"] = False
        self.assert_status("incomparable")

    def test_autoload_on_is_incomparable(self):
        self.samples[1]["response"]["settings"]["autoload_known_extensions"] = "true"
        self.assert_status("incomparable")

    def test_stock_httpfs_from_elsewhere_is_incomparable(self):
        stock = next(r for r in self.samples if r["variant"] == "stock")
        stock["response"]["extensions"][1]["install_path"] = "/root/.duckdb/extensions/httpfs.duckdb_extension"
        self.assert_status("incomparable", "stock: httpfs was loaded from")

    def test_custom_httpfs_not_built_in_is_incomparable(self):
        custom = next(r for r in self.samples if r["variant"] == "custom")
        custom["response"]["extensions"][1].update(install_mode="UNKNOWN", install_path=STOCK_HTTPFS)
        self.assert_status("incomparable", "custom: httpfs not built in")

    def test_other_url_is_incomparable(self):
        r = self.samples[0]["response"]
        r["url"] = r["url"].replace("fixtures:8080", "example.com")
        self.assert_status("incomparable", "http://example.com/v2/tiny.parquet")

    def test_other_fixture_is_incomparable(self):
        self.prov["fixture"]["sha256"] = "0" * 64
        self.assert_status("incomparable", "but the manifest says")

    def test_setup_status_wins(self):
        self.prov["setup"] = {"status": "incomparable", "reason": "no httpfs"}
        self.samples = []
        self.warmup = []
        summary = self.run_summary()
        self.assertEqual((summary["status"], summary["reasons"]), ("incomparable", ["no httpfs"]))

    def test_too_few_samples(self):
        self.samples = self.samples[:-2]
        self.assert_status("invalid samples", "3 valid samples of 4 requested")

    def test_remote_outcomes_do_not_change_the_status(self):
        ok = copy.deepcopy(self.record("stock", 1, 500))
        ok["response"].update(mode="remote", result=1249975000)
        failed = self.record("custom", 1, 501)
        failed["response"].update(mode="remote", error="IO Error: Could not resolve hostname", result_ok=False,
                                  result=None)
        self.remote = [ok, failed]
        summary = self.assert_status("valid")
        self.assertEqual(summary["remote"]["stock"], "passed")
        self.assertTrue(summary["remote"]["custom"].startswith("host or network failure"))

    def test_main_exit_status(self):
        self.run_summary()
        argv = sys.argv
        # Keep the test's output out of a CI job's summary.
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
        try:
            sys.argv = ["summarize.py", str(self.dir), str(MANIFEST)]
            with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(summarize.main(), 0)
                self.assertIn("| First HTTP Parquet query |", out.getvalue())
                self.samples[0]["response"]["result_ok"] = False
                self.run_summary()
                self.assertEqual(summarize.main(), 1)
        finally:
            sys.argv = argv


if __name__ == "__main__":
    unittest.main()
