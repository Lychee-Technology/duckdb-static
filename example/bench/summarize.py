#!/usr/bin/env python3
"""Checks and summarizes the results run.sh wrote.

Usage: summarize.py <results dir> <test/fixtures/public/v1/manifest.json>

Reads provenance.json, warmup.jsonl, samples.jsonl and remote.jsonl, writes
summary.md and summary.json next to them, prints summary.md, and appends it to
$GITHUB_STEP_SUMMARY when that is set. Exits 0 when the status is "valid" and
1 otherwise.

A sample counts only if it comes from a process no other sample came from, ran
the benchmark on its first invocation, and has every metric. The status is the
first that applies:

  not run             the variants weren't built
  functional failure  a variant failed or returned the wrong result
  incomparable        the variants differ in DuckDB version, configuration,
                      extension readiness or data, or the stock variant can't
                      load httpfs offline
  invalid samples     a sample was rejected, or fewer valid samples than asked
  valid               everything else

No status says which variant is faster, and none fails because one isn't.
"""

import json
import math
import os
import statistics
import sys
from pathlib import Path

VARIANTS = ("stock", "custom")
EXTENSIONS = ("core_functions", "httpfs", "icu", "json", "parquet")
FIXTURE = "multi_row_group.parquet"
EXPECTED_SUM = 1249975000

PRIMARY = (
    ("duckdb_open_or_connect_ms", "Open and connect"),
    ("extension_ready_ms", "Extension readiness (`LOAD` of 5)"),
    ("first_local_parquet_query_ms", "First local Parquet query"),
    ("duckdb_ready_and_first_local_query_ms", "Total"),
)
SECONDARY = (("bootstrap_to_main_ms", "Launcher exec to `main`"),)
METRICS = PRIMARY + SECONDARY

# Error text that points at the host or the network rather than the bundle.
NETWORK_ERRORS = (
    "could not resolve",
    "could not connect",
    "connection refused",
    "connection reset",
    "timed out",
    "timeout",
    "http 429",
    "http 5",
)


def load_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def check(record, seen):
    """Returns ("ok" | "functional" | "rejected", reason)."""
    where = f"{record['variant']} iteration {record['iteration']}"
    r = record.get("response")
    if not isinstance(r, dict):
        return "rejected", f"{where}: no response (sam local invoke exited {record.get('invoke_status')})"
    if "errorType" in r or "errorMessage" in r:
        return "functional", f"{where}: the function failed: {r.get('errorMessage', r.get('errorType'))}"
    if r.get("error"):
        return "functional", f"{where}: {r['error']}"
    if not r.get("result_ok"):
        return "functional", f"{where}: sum(id) was {r.get('result')}, not {EXPECTED_SUM}"
    if r.get("variant") != record["variant"]:
        return "rejected", f"{where}: the response came from variant {r.get('variant')!r}"
    if r.get("mode") != "bench":
        return "rejected", f"{where}: mode was {r.get('mode')!r}"
    instance = r.get("instance_id")
    if not instance or instance in seen:
        return "rejected", f"{where}: process {instance!r} was used before"
    seen.add(instance)
    if r.get("invocation") != 1:
        return "rejected", f"{where}: invocation {r.get('invocation')} of its process, not the first"
    for key, _ in METRICS:
        if not is_number(r.get(key)):
            return "rejected", f"{where}: {key} is missing or invalid"
    return "ok", ""


def parity(valid, fixture_sha256):
    """Returns the reasons the variants can't be compared."""
    reasons = []
    for v in VARIANTS:
        if not valid[v]:
            reasons.append(f"No valid {v} sample.")
    every = [r for v in VARIANTS for r in valid[v]]
    builds = sorted({f"{r.get('library_version')} {r.get('source_id')}" for r in every})
    if len(builds) > 1:
        reasons.append(f"The variants report different DuckDB builds: {', '.join(builds)}.")
    platforms = sorted({str(r.get("platform")) for r in every})
    if len(platforms) > 1:
        reasons.append(f"The variants report different platforms: {', '.join(platforms)}.")
    for v in VARIANTS:
        for r in valid[v]:
            states = {e.get("name"): e for e in r.get("extensions") or []}
            missing = [n for n in EXTENSIONS if not states.get(n, {}).get("loaded")]
            if missing:
                reasons.append(f"{v}: {', '.join(missing)} not loaded after the LOADs.")
                break
            settings = r.get("settings") or {}
            if settings.get("autoinstall_known_extensions") != "false" or settings.get("autoload_known_extensions") != "false":
                reasons.append(f"{v}: automatic extension installation or loading was on.")
                break
            if r.get("fixture_sha256") != fixture_sha256:
                reasons.append(f"{v}: the packaged {FIXTURE} doesn't match the manifest.")
                break
    return reasons


def stats(values):
    if len(values) == 1:
        q1 = p95 = q3 = values[0]
    else:
        cuts = statistics.quantiles(values, n=100, method="inclusive")
        q1, q3, p95 = cuts[24], cuts[74], cuts[94]
    return {
        "n": len(values),
        "median": statistics.median(values),
        "p95": p95,
        "iqr": q3 - q1,
        "min": min(values),
        "max": max(values),
    }


def remote_outcome(record):
    r = record.get("response")
    if not isinstance(r, dict):
        return "failed: no response"
    error = r.get("error") or r.get("errorMessage") or ""
    if not error and r.get("result_ok"):
        return "passed"
    if any(s in error.lower() for s in NETWORK_ERRORS):
        return f"host or network failure: {error}"
    if error:
        return f"failed: {error}"
    return f"failed: sum(id) was {r.get('result')}, not {EXPECTED_SUM}"


def summarize(results, manifest):
    prov = json.loads((results / "provenance.json").read_text())
    fixture_sha256 = next(f["sha256"] for f in manifest["files"] if f["path"] == FIXTURE)
    requested = prov.get("samples_requested", 0)

    seen = set()
    reasons = {"functional": [], "rejected": []}
    for record in load_jsonl(results / "warmup.jsonl"):
        kind, reason = check(record, seen)
        if kind != "ok":
            reasons.setdefault(kind, []).append(f"warm-up {reason}")
    valid = {v: [] for v in VARIANTS}
    for record in load_jsonl(results / "samples.jsonl"):
        kind, reason = check(record, seen)
        if kind == "ok":
            valid[record["variant"]].append(record["response"])
        else:
            reasons[kind].append(reason)

    setup = prov.get("setup")
    incomparable = parity(valid, fixture_sha256) if not setup else []
    for v in VARIANTS:
        if not setup and len(valid[v]) < requested:
            reasons["rejected"].append(f"{v}: {len(valid[v])} valid samples of {requested} requested.")
    if setup:
        status, why = setup["status"], [setup["reason"]]
    elif reasons["functional"]:
        status, why = "functional failure", reasons["functional"]
    elif incomparable:
        status, why = "incomparable", incomparable
    elif reasons["rejected"]:
        status, why = "invalid samples", reasons["rejected"]
    else:
        status, why = "valid", []

    summary = {
        "status": status,
        "reasons": why,
        "arch": prov.get("arch"),
        "samples_requested": requested,
        "valid_samples": {v: len(valid[v]) for v in VARIANTS},
        "metrics": {},
        "remote": {rec["variant"]: remote_outcome(rec) for rec in load_jsonl(results / "remote.jsonl")},
    }
    if status == "valid":
        for key, _ in METRICS:
            summary["metrics"][key] = {v: stats([r[key] for r in valid[v]]) for v in VARIANTS}
    return prov, valid, summary


def table(summary, metrics):
    lines = [
        "| Metric | Stock median | Stock p95 | Stock IQR | Custom median | Custom p95 | Custom IQR |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for key, label in metrics:
        s, c = summary["metrics"][key]["stock"], summary["metrics"][key]["custom"]
        lines.append(
            f"| {label} | {s['median']:.2f} | {s['p95']:.2f} | {s['iqr']:.2f} "
            f"| {c['median']:.2f} | {c['p95']:.2f} | {c['iqr']:.2f} |"
        )
    return lines


def markdown(prov, valid, summary):
    arch = summary["arch"]
    n = summary["valid_samples"]
    out = [f"## DuckDB readiness benchmark ({arch})", ""]
    out.append(
        f"**Status: {summary['status']}.** Valid samples: {n['stock']} stock, {n['custom']} custom, "
        f"of {summary['samples_requested']} requested per variant."
    )
    out.append("")
    if summary["reasons"]:
        out += [f"- {reason}" for reason in summary["reasons"][:20]]
        if len(summary["reasons"]) > 20:
            out.append(f"- … and {len(summary['reasons']) - 20} more")
        out.append("")
    if summary["status"] == "valid":
        out += [
            "Each sample is a new process in a new container under `sam local invoke`, on the runner described below. "
            "These are not managed Lambda cold starts, and no threshold applies. Milliseconds:",
            "",
            "### Primary: inside the process",
            "",
            *table(summary, PRIMARY),
            "",
            "### Secondary: process start",
            "",
            *table(summary, SECONDARY),
            "",
            "Measured with `CLOCK_MONOTONIC` from just before the launcher's `exec` to the first statement of `main`. "
            "It includes loading the executable and its libraries, static constructors, the Go runtime and package "
            "initialization, and isn't any one of them.",
            "",
        ]

    out += ["### Informational", ""]
    first = (valid["stock"] or valid["custom"] or [{}])[0]
    if first:
        out.append(f"- DuckDB: {first.get('library_version')} ({first.get('source_id')}), {first.get('platform')}")
    bundle = prov.get("bundle") or {}
    if bundle:
        out.append(f"- Bundle: `{bundle.get('name')}`, sha256 `{bundle.get('sha256')}`")
    ext = prov.get("stock_httpfs")
    if ext:
        out.append(
            f"- Stock httpfs, provisioned before any sample and not timed in them: {ext['url']}, "
            f"{ext['gz_bytes']} bytes downloaded in {ext['download_seconds']:.2f} s, {ext['bytes']} bytes unpacked, "
            f"sha256 `{ext['sha256']}`"
        )
    sizes = prov.get("sizes")
    if sizes:
        out += [
            "",
            "| Bytes | Stock | Custom |",
            "|---|---:|---:|",
            f"| Executable | {sizes['stock']['executable_bytes']} | {sizes['custom']['executable_bytes']} |",
            f"| Package, unzipped | {sizes['stock']['package_bytes']} | {sizes['custom']['package_bytes']} |",
            f"| Package, zipped | {sizes['stock']['package_zip_bytes']} | {sizes['custom']['package_zip_bytes']} |",
            "",
        ]
    if valid["stock"] and valid["custom"]:
        modes = {v: {e["name"]: e.get("install_mode") for e in valid[v][0].get("extensions") or []} for v in VARIANTS}
        out += ["| Extension | Stock | Custom |", "|---|---|---|"]
        out += [f"| {name} | {modes['stock'].get(name)} | {modes['custom'].get(name)} |" for name in EXTENSIONS]
        out.append("")
    for line in prov.get("build") or []:
        out.append(f"- Build: {line}")
    runner = prov.get("runner")
    if runner:
        out.append(f"- Runner: {runner.get('cpu')}, {runner.get('cpus')} CPUs, {runner.get('kernel')}; "
                   f"Docker {runner.get('docker')}; {runner.get('sam')}")
    out.append("")

    out += ["### HTTPS check (untimed)", ""]
    if summary["remote"]:
        out += [f"- {v}: {outcome}" for v, outcome in summary["remote"].items()]
    else:
        out.append("- Not configured: `DUCKDB_TEST_HTTPFS_BASE_URL` is unset.")
    out.append("")
    return "\n".join(out)


def main():
    if len(sys.argv) != 3:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    results = Path(sys.argv[1])
    manifest = json.loads(Path(sys.argv[2]).read_text())
    prov, valid, summary = summarize(results, manifest)
    text = markdown(prov, valid, summary)
    (results / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (results / "summary.md").write_text(text)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text)
    return 0 if summary["status"] == "valid" else 1


if __name__ == "__main__":
    sys.exit(main())
