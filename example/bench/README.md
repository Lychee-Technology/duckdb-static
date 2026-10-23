# DuckDB readiness benchmark

How long DuckDB takes, inside a fresh Lambda process, to open a database, make the bundle's extensions ready and answer a first query on a local Parquet file. The benchmark compares duckdb-go's stock prebuilt library with this repository's bundle. It characterizes both; it doesn't set out to show that the bundle is faster, and a run where it isn't is still a valid run. The decisions behind it are recorded in [#33](https://github.com/Lychee-Technology/duckdb-static/issues/33).

It runs under `sam local invoke` on `provided.al2023`, on a GitHub-hosted runner or your own machine. That is SAM's emulator, not managed AWS Lambda, so none of the numbers is a Lambda cold-start duration.

## The two variants

Both are built from `main.go` by one Dockerfile, with the `duckdb-go` version in `example/go.mod`, the same Go and compiler versions and the same Go build flags. They run with the same SAM template settings (512 MB, 30 s) and the same SQL.

| | Stock | Custom |
|---|---|---|
| DuckDB library | duckdb-go's prebuilt `go.duckdb.org/libduckdb/linux-<arch>`, built by DuckDB | The bundle under test, linked with `-tags=duckdb_use_static_lib` |
| Built-in extensions | `core_functions`, `parquet`, `json`, `icu`, plus `tpch`, `tpcds`, `autocomplete` and DuckDB's `httplib` and loadable-extension support | `core_functions`, `parquet`, `json`, `icu`, `httpfs` |
| `httpfs` | DuckDB's own build for the same version, downloaded from `extensions.duckdb.org` before any sample and packaged in `ext/` | Built in |

The stock library has no `httpfs`, so it is the one extension whose readiness differs. Both variants open DuckDB with `autoinstall_known_extensions` and `autoload_known_extensions` off and `extension_directory` in the package, so nothing is downloaded or loaded behind the program's back. Both then run `LOAD` for each of the five extensions. For an extension that is built in, `LOAD` does nothing; in the stock variant, `LOAD httpfs` reads the packaged file from local disk.

If the stock library isn't the DuckDB version in `duckdb_version`, or DuckDB hasn't published `httpfs` for it, the run reports `incomparable` rather than comparing different work.

## What is measured

Each sample is one invocation of a new process in a new container. The function takes all its timings itself, with Go's monotonic clock, and returns them with what it ran.

**Primary**, inside the process, in this order:

| Metric | From | To |
|---|---|---|
| `duckdb_open_or_connect_ms` | before `duckdb.NewConnector`, which opens the database | after the first connection. `sql.Open` would open the database right away but connect lazily, so the function calls both steps itself. |
| `extension_ready_ms` | after connecting | after `LOAD` of `core_functions`, `parquet`, `json`, `icu` and `httpfs` |
| `first_local_parquet_query_ms` | after the `LOAD`s | after reading the one-row result of `SELECT sum(id) FROM read_parquet('/var/task/data/multi_row_group.parquet')`, which must be `1249975000` |
| `duckdb_ready_and_first_local_query_ms` | the start of the first | the end of the third |

The file is `test/fixtures/public/v1/multi_row_group.parquet`: 1,087,892 bytes, 50,000 rows in 5 row groups. Its sha256 must match the fixtures' manifest.

**Secondary:** `bootstrap_to_main_ms`. The function's `bootstrap` is a small C launcher (`launcher/launcher.c`). It reads `CLOCK_MONOTONIC`, passes the reading on, and execs the Go program in the same process, which reads the same clock as the first statement of `main`. The interval covers loading the executable and its shared libraries, C++ static constructors, the Go runtime, and package initialization, which includes duckdb-go registering the static extensions. It isn't any one of these, and it isn't Lambda's `Init Duration`: it misses everything before the launcher's first instruction, including the container and the runtime emulator starting. Never add it to the primary total.

**Informational**, in `provenance.json` and the summary:

- The DuckDB version and source commit each variant reports. They must be the same.
- The bundle's sha256, and the stock `httpfs` file: its URL, size, sha256 and how long it took to download. That download happens once, before any sample, and is in no timed metric.
- Each variant's executable, package and zipped package size.
- Each extension's `install_mode` after the `LOAD`s.
- The Go and compiler versions and flags.
- The runner's CPU, kernel, Docker and SAM versions.

## How a run is judged

`run.sh` invokes each variant once to warm up, which also builds SAM's runtime image. That sample isn't counted. It then takes the samples in pairs, stock first in odd iterations and custom first in even ones. `summarize.py` checks every response, and the first of these that applies is the run's status:

| Status | Meaning |
|---|---|
| `not run` | The variants couldn't be built. |
| `functional failure` | A variant returned an error or a wrong `sum(id)`. |
| `incomparable` | The stock library in `example/go.mod` is a different DuckDB version from `duckdb_version`, the stock variant can't get `httpfs`, the variants report different DuckDB builds or platforms, an extension isn't loaded, automatic loading was on, or the fixture's hash is wrong. |
| `invalid samples` | A sample came from a process that had answered before (by its random instance ID, not its PID, which repeats across containers), wasn't its process's first invocation, or lacks a metric; or a variant has fewer valid samples than were asked for. |
| `valid` | None of these. The summary then gives each metric's median, p95 and interquartile range per variant. |

`run.sh` exits 0 only for `valid`. There is no performance threshold, and none should be added until the variance across runs is known.

When `DUCKDB_TEST_HTTPFS_BASE_URL` is set, each variant is also invoked once more to read the same fixture over HTTPS from it. That check is untimed and separate from the samples, and its outcome doesn't change the status.

## Running it

You need Docker or Podman, the AWS SAM CLI, `jq` and Python 3, on a host of the bundle's architecture: x86_64 for `amd64`, aarch64 for `arm64`. Don't run the other architecture under emulation. Take a bundle tarball from a build run or a release (see [`test/README.md`](../../test/README.md#running-the-tests)), and from the repository's root:

```bash
BUNDLE_TARBALL=$HOME/duckdb-bundle/libduckdb_bundle-amd64-linux-httpfs-parquet.tar.xz \
SAMPLES=5 BENCH_WORK=$HOME/duckdb-bench \
  example/bench/run.sh
```

`SAMPLES` defaults to 30. The results are in `$BENCH_WORK/results/`: `summary.md`, `summary.json`, `provenance.json`, the raw `samples.jsonl`, `warmup.jsonl` and `remote.jsonl`, and the build and SAM logs. Set `DUCKDB_TEST_HTTPFS_BASE_URL` as in [`test/README.md`](../../test/README.md#real-https) to add the HTTPS check. `run.sh` extracts the bundle into `example/bench/libs/`, which git ignores.

On Fedora with rootless Podman, point SAM at a Podman API socket with SELinux labeling off, as [`example/AGENTS.md`](../AGENTS.md#podman) describes.

## In CI

`.github/workflows/benchmark.yml` runs `run.sh` on `ubuntu-24.04-arm` and `ubuntu-latest`, uploads `results/` as `benchmark-<arch>` (kept 90 days), and puts the summary in the job summary. `build-and-release.yml` calls it with the bundles the run just built: in runs on `main`, which today means a dispatch because pushes to `main` don't build, and on tags; on another branch only when the dispatch sets `benchmark`; and never on a PR. The dispatch input `benchmark-samples` sets the sample count. The release doesn't wait for it. To benchmark an earlier build run's bundles again, within a day of that run:

```bash
gh workflow run benchmark.yml -f build-run-id=<run id> -f samples=30
```

Shared runners are noisy, and nothing in a single run tells you how noisy. Compare runs before reading much into a difference of a few milliseconds.
