#!/usr/bin/env python3
"""Tests summarize.py's status decisions on synthetic results.

Usage: python3 example/bench/test_summarize.py [-v]
"""

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import summarize  # noqa: E402

MANIFEST = Path(HERE, "..", "..", "test", "fixtures", "public", "v1", "manifest.json")
FIXTURE_SHA256 = next(
    f["sha256"] for f in json.loads(MANIFEST.read_text())["files"] if f["path"] == "multi_row_group.parquet"
)


def response(variant, n):
    return {
        "mode": "bench",
        "variant": variant,
        "instance_id": f"{variant}-{n}",
        "invocation": 1,
        "pid": 1,
        "bootstrap_to_main_ms": 40.0 + n,
        "duckdb_open_or_connect_ms": 20.0 + n,
        "extension_ready_ms": 1.0,
        "first_local_parquet_query_ms": 2.0,
        "duckdb_ready_and_first_local_query_ms": 23.0 + n,
        "result": 1249975000,
        "result_ok": True,
        "error": "",
        "library_version": "v2.0.0",
        "source_id": "d54cbbe3df",
        "platform": "linux_amd64",
        "extensions": [
            {"name": name, "loaded": True, "installed": True, "install_mode": "STATICALLY_LINKED"}
            for name in summarize.EXTENSIONS
        ],
        "settings": {
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
            "extension_directory": "/var/task/ext",
        },
        "fixture_sha256": FIXTURE_SHA256,
        "fixture_bytes": 1087892,
        "executable_bytes": 1,
    }


class SummarizeTest(unittest.TestCase):
    SAMPLES = 4

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.prov = {"arch": "amd64", "samples_requested": self.SAMPLES}
        self.warmup = [self.record("stock", 0, 1000), self.record("custom", 0, 1000)]
        self.samples = [
            self.record(v, i, i) for i in range(1, self.SAMPLES + 1) for v in summarize.VARIANTS
        ]
        self.remote = []

    def record(self, variant, iteration, n):
        return {
            "variant": variant,
            "iteration": iteration,
            "position": 1,
            "invoke_status": 0,
            "response": response(variant, n),
        }

    def run_summary(self):
        for name, rows in (("warmup", self.warmup), ("samples", self.samples), ("remote", self.remote)):
            (self.dir / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        (self.dir / "provenance.json").write_text(json.dumps(self.prov))
        manifest = json.loads(MANIFEST.read_text())
        prov, valid, summary = summarize.summarize(self.dir, manifest)
        summarize.markdown(prov, valid, summary)  # must not raise
        return summary

    def test_valid(self):
        summary = self.run_summary()
        self.assertEqual(summary["status"], "valid")
        self.assertEqual(summary["valid_samples"], {"stock": 4, "custom": 4})
        total = summary["metrics"]["duckdb_ready_and_first_local_query_ms"]["stock"]
        self.assertEqual((total["n"], total["median"], total["min"], total["max"]), (4, 25.5, 24.0, 27.0))

    def test_no_faster_variant_is_still_valid(self):
        for r in self.samples:
            r["response"]["duckdb_ready_and_first_local_query_ms"] = 99.0 if r["variant"] == "custom" else 1.0
        self.assertEqual(self.run_summary()["status"], "valid")

    def test_reused_process_is_rejected(self):
        self.samples[3]["response"]["instance_id"] = self.samples[1]["response"]["instance_id"]
        summary = self.run_summary()
        self.assertEqual(summary["status"], "invalid samples")
        self.assertIn("used before", " ".join(summary["reasons"]))

    def test_warmup_process_reused_is_rejected(self):
        self.samples[0]["response"]["instance_id"] = self.warmup[0]["response"]["instance_id"]
        self.assertEqual(self.run_summary()["status"], "invalid samples")

    def test_second_invocation_is_rejected(self):
        self.samples[2]["response"]["invocation"] = 2
        self.assertEqual(self.run_summary()["status"], "invalid samples")

    def test_missing_metric_is_rejected(self):
        del self.samples[0]["response"]["bootstrap_to_main_ms"]
        summary = self.run_summary()
        self.assertEqual(summary["status"], "invalid samples")
        self.assertIn("bootstrap_to_main_ms", " ".join(summary["reasons"]))

    def test_null_metric_is_rejected(self):
        self.samples[0]["response"]["bootstrap_to_main_ms"] = None
        self.assertEqual(self.run_summary()["status"], "invalid samples")

    def test_no_response_is_rejected(self):
        self.samples[0]["response"] = None
        self.samples[0]["invoke_status"] = 1
        self.assertEqual(self.run_summary()["status"], "invalid samples")

    def test_wrong_result_is_a_functional_failure(self):
        self.samples[0]["response"]["result_ok"] = False
        self.samples[0]["response"]["result"] = 1
        self.assertEqual(self.run_summary()["status"], "functional failure")

    def test_function_error_is_a_functional_failure(self):
        self.samples[0]["response"] = {"errorType": "Runtime.ExitError", "errorMessage": "exit status 2"}
        self.assertEqual(self.run_summary()["status"], "functional failure")

    def test_different_duckdb_builds_are_incomparable(self):
        for r in self.samples:
            if r["variant"] == "stock":
                r["response"]["source_id"] = "0000000000"
        self.assertEqual(self.run_summary()["status"], "incomparable")

    def test_unloaded_extension_is_incomparable(self):
        self.samples[1]["response"]["extensions"][1]["loaded"] = False
        self.assertEqual(self.run_summary()["status"], "incomparable")

    def test_autoload_on_is_incomparable(self):
        self.samples[1]["response"]["settings"]["autoload_known_extensions"] = "true"
        self.assertEqual(self.run_summary()["status"], "incomparable")

    def test_other_fixture_is_incomparable(self):
        self.samples[0]["response"]["fixture_sha256"] = "0" * 64
        self.assertEqual(self.run_summary()["status"], "incomparable")

    def test_setup_status_wins(self):
        self.prov["setup"] = {"status": "incomparable", "reason": "no httpfs"}
        self.samples = []
        self.warmup = []
        summary = self.run_summary()
        self.assertEqual((summary["status"], summary["reasons"]), ("incomparable", ["no httpfs"]))

    def test_too_few_samples(self):
        self.samples = self.samples[:-2]
        self.assertEqual(self.run_summary()["status"], "invalid samples")

    def test_remote_outcomes_do_not_change_the_status(self):
        ok = copy.deepcopy(self.record("stock", 1, 500))
        ok["response"]["mode"] = "remote"
        failed = self.record("custom", 1, 501)
        failed["response"].update(error="IO Error: Could not resolve hostname", result_ok=False, result=None)
        self.remote = [ok, failed]
        summary = self.run_summary()
        self.assertEqual(summary["status"], "valid")
        self.assertEqual(summary["remote"]["stock"], "passed")
        self.assertTrue(summary["remote"]["custom"].startswith("host or network failure"))

    def test_main_exit_status(self):
        self.run_summary()
        argv = sys.argv
        # Keep the test's output out of a CI job's summary.
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
        try:
            sys.argv = ["summarize.py", str(self.dir), str(MANIFEST)]
            with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(summarize.main(), 0)
                self.samples[0]["response"]["result_ok"] = False
                self.run_summary()
                self.assertEqual(summarize.main(), 1)
        finally:
            sys.argv = argv


if __name__ == "__main__":
    unittest.main()
