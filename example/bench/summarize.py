#!/usr/bin/env python3
"""Checks and summarizes the results run.sh wrote.

Usage: summarize.py <results dir> <test/fixtures/public/v2/manifest.json>

Reads provenance.json, warmup.jsonl, samples.jsonl, remote.jsonl and the
fixture server's fixture-requests.jsonl, writes summary.md and summary.json
next to them, prints summary.md, and appends it to $GITHUB_STEP_SUMMARY when
that is set. Exits 0 when the status is "valid" and 1 otherwise.

A sample counts only if it comes from a process no other sample came from, ran
the benchmark on its first invocation, has every metric, and the fixture
server logged its reads of the fixture, each answered with 200 or 206. The
status is the first that applies:

  not run             the variants weren't built, or the fixture server or a
                      warm-up failed before any sample
  functional failure  a variant failed or returned the wrong result
  incomparable        the variants differ in DuckDB version, configuration,
                      extension readiness, how they load httpfs, or data, a
                      variant's DuckDB isn't duckdb_version's, or the stock
                      variant can't load httpfs offline
  invalid samples     a sample was rejected, a variant has fewer valid samples
                      than asked, or the fixture server failed during sampling
  valid               everything else

No status says which variant is faster, and none fails because one isn't.
"""

import collections
import json
import math
import os
import statistics
import sys
import urllib.parse
from pathlib import Path

VARIANTS = ("stock", "custom")
EXTENSIONS = ("core_functions", "httpfs", "icu", "json", "parquet")
FIXTURE = "tiny.parquet"
# The benchmark's query, whose expected result the manifest records.
QUERY = f"SELECT sum(id) FROM read_parquet('{{dir}}/{FIXTURE}')"
# sum(id) of v1/multi_row_group.parquet, which the HTTPS check reads.
REMOTE_EXPECTED_SUM = 1249975000

PRIMARY = (
    ("duckdb_open_or_connect_ms", "Open and connect"),
    ("extension_ready_ms", "Extension readiness (`LOAD` of 5)"),
    ("first_http_parquet_query_ms", "First HTTP Parquet query"),
    ("duckdb_ready_and_first_http_query_ms", "Total"),
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


def load_requests(path):
    """Returns the fixture server's requests by the instance ID in their query
    string, each instance's in the order they started. Lines that aren't JSON
    objects, such as a container runtime's notices, are skipped."""
    by_instance = collections.defaultdict(list)
    if not path.exists():
        return by_instance
    for line in path.read_text().splitlines():
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        instance = urllib.parse.parse_qs(request.get("query") or "").get("instance", [None])[0]
        if instance:
            by_instance[instance].append(request)
    # The server logs a request when it has answered it, so a request can be
    # logged after the one its client sent next.
    for requests in by_instance.values():
        requests.sort(key=lambda r: r["time"] if is_number(r.get("time")) else 0)
    return by_instance


def expected_sum(manifest):
    return int(next(q["expected"] for q in manifest["queries"] if q["sql"] == QUERY))


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def check_requests(requests, path):
    """Returns why a sample's logged requests don't show a clean read of the
    fixture at path, or "" if they do. It sets no limit on their number or
    their time."""
    if not requests:
        return "the fixture server logged no request from its process"
    for request in requests:
        if request.get("path") != path:
            return f"its process requested {request.get('path')}, not {path}"
        if request.get("status") not in (200, 206):
            what = " ".join(filter(None, (request.get("method"), request.get("range"))))
            return f"the fixture server answered {what} with {request.get('status')}"
    if not any(r.get("method") == "GET" and r.get("bytes", 0) > 0 for r in requests):
        return "the fixture server sent its process no fixture bytes"
    return ""


def check(record, seen, expected, requests=None, path=None):
    """Returns ("ok" | "functional" | "rejected", reason). requests are the
    fixture server's log entries for the sample's process, or None not to
    check them."""
    where = f"{record['variant']} iteration {record['iteration']}"
    r = record.get("response")
    if not isinstance(r, dict):
        return "rejected", f"{where}: no response (sam local invoke exited {record.get('invoke_status')})"
    if "errorType" in r or "errorMessage" in r:
        return "functional", f"{where}: the function failed: {r.get('errorMessage', r.get('errorType'))}"
    if r.get("error"):
        return "functional", f"{where}: {r['error']}"
    if not r.get("result_ok") or r.get("result") != expected:
        return "functional", f"{where}: sum(id) was {r.get('result')}, not {expected}"
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
    if requests is not None:
        problem = check_requests(requests.get(instance, []), path)
        if problem:
            return "rejected", f"{where}: {problem}"
    return "ok", ""


def base_url(url):
    """url without its "instance" query parameter."""
    parts = urllib.parse.urlsplit(url or "")
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k != "instance"]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def parity(valid, prov, manifest):
    """Returns the reasons the variants can't be compared. A variant without
    valid samples isn't one of them: its rejected samples say why."""
    reasons = []
    fixture = prov.get("fixture") or {}
    committed = next((f for f in manifest["files"] if f["path"] == FIXTURE), {})
    if fixture.get("sha256") != committed.get("sha256"):
        reasons.append(f"The served {FIXTURE} has sha256 {fixture.get('sha256')}, "
                       f"but the manifest says {committed.get('sha256')}.")
    every = [r for v in VARIANTS for r in valid[v]]
    urls = sorted({base_url(r.get("url")) for r in every})
    if urls and urls != [fixture.get("url")]:
        reasons.append(f"The samples read {', '.join(urls)}, not the fixture server's {fixture.get('url')}.")
    versions = sorted({str(r.get("library_version")) for r in every})
    if prov.get("duckdb_version") and versions and versions != [prov["duckdb_version"]]:
        reasons.append(f"The samples report DuckDB {', '.join(versions)}, but duckdb_version is {prov['duckdb_version']}.")
    # DuckDB abbreviates its source commit to a length that depends on the
    # build: for v1.5.6 the bundle reports 069cc9f and duckdb-go-bindings'
    # library 069cc9f9b5. Every ID must be a prefix of the longest.
    ids = sorted({str(r.get("source_id") or "") for r in every}, key=len)
    if len(versions) > 1 or any(len(i) < 7 or not ids[-1].startswith(i) for i in ids):
        builds = sorted({f"{r.get('library_version')} {r.get('source_id')}" for r in every})
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
            # The comparison is the bundle's built-in httpfs against the stock
            # library's with DuckDB's httpfs loaded from the package.
            if v == "custom":
                not_built_in = [n for n in EXTENSIONS if states[n].get("install_mode") != "STATICALLY_LINKED"]
                if not_built_in:
                    reasons.append(f"custom: {', '.join(not_built_in)} not built in.")
                    break
            else:
                directory = (settings.get("extension_directory") or "").rstrip("/") + "/"
                path = states["httpfs"].get("install_path") or ""
                if directory == "/" or not path.startswith(directory):
                    reasons.append(f"stock: httpfs was loaded from {path!r}, not from the package's "
                                   f"{settings.get('extension_directory')!r}.")
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


def http_stats(responses, requests):
    """Summarizes the fixture server's log for one variant's valid samples."""
    per_sample = [requests.get(r["instance_id"], []) for r in responses]
    sequences = collections.Counter(
        tuple(f"{q.get('method')} {q.get('range') or '-'} {q.get('status')} {q.get('bytes')}" for q in sample)
        for sample in per_sample
    )
    return {
        "requests": stats([len(sample) for sample in per_sample]),
        "bytes": stats([sum(q.get("bytes", 0) for q in sample) for sample in per_sample]),
        "server_ms": stats([sum(q.get("duration_ms", 0) for q in sample) for sample in per_sample]),
        "sequences": [{"requests": list(seq), "samples": n} for seq, n in sequences.most_common()],
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
    return f"failed: sum(id) was {r.get('result')}, not {REMOTE_EXPECTED_SUM}"


def summarize(results, manifest):
    prov = json.loads((results / "provenance.json").read_text())
    expected = expected_sum(manifest)
    path = f"/{manifest['version']}/{FIXTURE}"
    requests = load_requests(results / "fixture-requests.jsonl")
    requested = prov.get("samples_requested", 0)

    seen = set()
    reasons = {"functional": [], "rejected": []}
    for record in load_jsonl(results / "warmup.jsonl"):
        kind, reason = check(record, seen, expected)
        if kind != "ok":
            reasons.setdefault(kind, []).append(f"warm-up {reason}")
    valid = {v: [] for v in VARIANTS}
    for record in load_jsonl(results / "samples.jsonl"):
        kind, reason = check(record, seen, expected, requests, path)
        if kind == "ok":
            valid[record["variant"]].append(record["response"])
        else:
            reasons[kind].append(reason)

    setup = prov.get("setup")
    incomparable = parity(valid, prov, manifest) if not setup else []
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
        "http": {},
        "remote": {rec["variant"]: remote_outcome(rec) for rec in load_jsonl(results / "remote.jsonl")},
    }
    if status == "valid":
        for key, _ in METRICS:
            summary["metrics"][key] = {v: stats([r[key] for r in valid[v]]) for v in VARIANTS}
        summary["http"] = {v: http_stats(valid[v], requests) for v in VARIANTS}
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


def http_table(summary):
    h = summary["http"]
    lines = ["| | Stock | Custom |", "|---|---|---|"]
    for key, label, fmt in (("requests", "Requests", "{:.0f}"), ("bytes", "Body bytes sent", "{:.0f}"),
                            ("server_ms", "Server time, ms", "{:.2f}")):
        cells = []
        for v in VARIANTS:
            s = h[v][key]
            cells.append(f"{fmt.format(s['median'])} (min {fmt.format(s['min'])}, max {fmt.format(s['max'])})")
        lines.append(f"| {label}, median | {cells[0]} | {cells[1]} |")
    lines.append("")
    lines.append("Most common request sequence, as method, Range, status and body bytes:")
    lines.append("")
    for v in VARIANTS:
        top = h[v]["sequences"][0]
        lines.append(f"- {v}, {top['samples']} of {h[v]['requests']['n']} samples: "
                     + "; ".join(f"`{q}`" for q in top["requests"]))
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
        fixture = prov.get("fixture") or {}
        out += [
            "Each sample is a new process in a new container under `sam local invoke`, on the runner described below. "
            f"Its first query reads `{fixture.get('path')}` ({fixture.get('bytes')} bytes) over HTTP from a fixture "
            "server on the same Docker network, which runs for the whole run. "
            "These are not managed Lambda cold starts, and no threshold applies. Milliseconds:",
            "",
            "### Primary: inside the process",
            "",
            *table(summary, PRIMARY),
            "",
            "The first query's time includes httpfs's first use, every HTTP request it makes and the query's "
            "execution, so not all of a difference in it or in the total comes from making the extensions ready.",
            "",
            "### Secondary: process start",
            "",
            *table(summary, SECONDARY),
            "",
            "Measured with `CLOCK_MONOTONIC` from just before the launcher's `exec` to the first statement of `main`. "
            "It includes loading the executable and its libraries, static constructors, the Go runtime and package "
            "initialization, and isn't any one of them. DuckDB 1.5.6 loads its linked extensions when the database "
            "opens, inside the first primary metric, not here. The primary metrics start after it.",
            "",
            "### HTTP requests per sample",
            "",
            "From the fixture server's log, matched to samples by the instance ID in each request's query string. "
            "The server's time runs from having read a request's headers to having written its body.",
            "",
            *http_table(summary),
            "",
        ]

    out += ["### Informational", ""]
    first = (valid["stock"] or valid["custom"] or [{}])[0]
    if first:
        out.append(f"- DuckDB: {first.get('library_version')} ({first.get('source_id')}), {first.get('platform')}")
    bundle = prov.get("bundle") or {}
    if bundle:
        out.append(f"- Bundle: `{bundle.get('name')}`, sha256 `{bundle.get('sha256')}`")
    lib = prov.get("stock_library")
    if lib:
        static = next((a.get("sha256") for a in lib.get("archives") or [] if a.get("name") == "libduckdb_static.a"), None)
        out.append(
            f"- Stock library: `{lib.get('module')}` {lib.get('version')} (`{lib.get('sum')}`), DuckDB "
            f"{lib.get('duckdb_version')} by its version, `libduckdb_static.a` sha256 `{static}`. The module ships "
            "no `provenance.json`, so the samples' `pragma_version()` is what identifies its DuckDB build."
        )
    needed = prov.get("needed")
    if needed:
        out.append("- Shared libraries each executable needs: "
                   + "; ".join(f"{v} {', '.join(needed.get(v) or [])}" for v in VARIANTS))
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
    fixture = prov.get("fixture")
    if fixture:
        out.append(f"- Fixture: {fixture.get('url')}, {fixture.get('bytes')} bytes, sha256 `{fixture.get('sha256')}`")
    server = prov.get("fixture_server")
    if server:
        out.append(f"- Fixture server: `test/fixtures/serve.py` in `{server.get('image')}` "
                   f"(image `{server.get('image_id')}`), network `{server.get('network')}`")
    if valid["stock"] and valid["custom"]:
        def how(e):
            mode = e.get("install_mode")
            return mode if mode == "STATICALLY_LINKED" or not e.get("install_path") else f"{mode}, `{e['install_path']}`"
        modes = {v: {e["name"]: how(e) for e in valid[v][0].get("extensions") or []} for v in VARIANTS}
        # Extensions loaded beyond the five, which DuckDB loaded on open
        # because the variant's library links them.
        others = sorted(set(modes["stock"]).union(modes["custom"]).difference(EXTENSIONS))
        out += ["", "| Extension | Stock | Custom |", "|---|---|---|"]
        out += [f"| {name} | {modes['stock'].get(name, '-')} | {modes['custom'].get(name, '-')} |"
                for name in EXTENSIONS + tuple(others)]
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
