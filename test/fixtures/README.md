# Parquet fixtures

Small Parquet files with known contents, for testing httpfs reads of a real HTTPS endpoint. The files are committed here, and GitHub serves them over HTTPS from `raw.githubusercontent.com`, so a test can run the same queries against a local server and against GitHub. A failure on GitHub alone points at the host or the network, and a failure on both points at the bundle. `test/smoke.sh` runs the v1 queries against its local server, and against GitHub as well when `DUCKDB_TEST_HTTPFS_BASE_URL` is set; see [`test/README.md`](../README.md). The benchmark in [`example/bench/`](../../example/bench/README.md) reads the v2 file over HTTP from the same local server, `serve.py`, and never from GitHub.

On `duckdb-v1.5.x`, everything in this directory is the same as on `main`, apart from this file.

| Path | What it is |
|---|---|
| `public/` | The files tests read. `public/v1/` holds the v1 files and `manifest.json`, `public/v2/` the v2 ones. |
| `v1.sql`, `v2.sql` | Write the files of each version. |
| `v1.queries.sql`, `v2.queries.sql` | The queries and the exact result each must give. |
| `fixtures.py` | Generates, checks and verifies the files. Python 3.9 or later, standard library only. |
| `serve.py` | Serves files over HTTP with byte ranges and logs each request, as `test/smoke.sh` and `example/bench/run.sh` need. Its docstring has the details. |
| `test_fixtures.py` | Tests `fixtures.py verify` against a local server, `generate`'s refusal to overwrite a version, `check`'s report of changed bytes, the `DUCKDB_CLI` override, and `serve.py`'s responses and logs, including that `verify` passes against it. The `check` test downloads the pinned CLI unless `DUCKDB_CLI` is set. |

## The v1 contract

| File | Rows | Row groups | Covers |
|---|---|---|---|
| `basic.parquet` | 100 | 1 | INTEGER, VARCHAR, DECIMAL, BOOLEAN, DATE |
| `nullable.parquet` | 1000 | 1 | NULLs in some rows and in every row |
| `nested.parquet` | 100 | 1 | lists with NULL elements, empty and NULL lists, structs, NULL structs, maps |
| `multi_row_group.parquet` | 50000 | 5 | predicates and projections that skip row groups and column chunks |

The v1 files total 1.1 MB. `manifest.json` records each file's size, sha256, row count, row groups and schema, and the queries from `v1.queries.sql`. Each query returns one row and one column, and its `expected` value is that column cast to VARCHAR. `duckdb_value_varchar` returns NULL for a list, so `test/smoke.c` casts in SQL. In each query, replace `{dir}` with the directory that holds the files: a local path, or `<base URL>/v1`. `absent` names a file that doesn't exist, which the host must answer with 404.

The manifest's hashes are the contract. Once a version is committed, its files never change, so a test pinned to `v1` keeps meaning the same thing. New or different data, or a different DuckDB, makes a new version (`v2.sql`, `v2.queries.sql` and a `VERSIONS` entry in `fixtures.py`) alongside the old one. `fixtures.py generate` refuses to overwrite an existing version. `test/smoke.sh` keeps reading v1 until `test/smoke.c` moves to the new version: its `FIXTURES` names the version, and `check_fixtures` expects that version's query count and its two queries that look up one id in `multi_row_group.parquet`.

## The v2 contract

| File | Rows | Row groups | Covers |
|---|---|---|---|
| `tiny.parquet` | 1200 | 1 | a file of about 10 KB, for the benchmark's first query over HTTP |

`tiny.parquet` has columns `id BIGINT` (1 to 1200) and `bucket INTEGER` (`id % 7`), uncompressed: 10,541 bytes. `SELECT sum(id)` over it gives `720600`, the benchmark's expected result, and has to decode every page of `id`, most of the file. v2 adds a file rather than changing v1, which keeps its meaning for `test/smoke.c` and the public host.

## Generating and checking

`fixtures.py` writes the files with the official DuckDB CLI pinned for each version in `VERSIONS`: v1 and v2 use DuckDB v2.0.0, independent of `duckdb_version`. It downloads the CLI for the host's architecture (x86_64 or aarch64) and checks its sha256. To avoid the download, set `DUCKDB_CLI` to a CLI that reports the same version.

```bash
python3 test/fixtures/fixtures.py check v1      # regenerate and compare with public/v1
python3 test/fixtures/fixtures.py check         # the same for every version in public/
python3 test/fixtures/fixtures.py generate v3   # write a new version
python3 test/fixtures/test_fixtures.py
```

Generation runs single-threaded, with extension autoloading off, and writes uncompressed files, so the bytes don't depend on the machine or the network. A compressor isn't guaranteed to write the same bytes on every architecture; snappy, DuckDB's default, doesn't on arm64. It checks every query in `<version>.queries.sql` against the new files and fails with the expected and actual values if one differs. `.github/workflows/fixtures.yml` runs `check` for every version and the tests on native arm64 and x86_64 runners.

## Hosting

There is nothing to deploy. GitHub serves every committed file, and the base URL pins a commit:

```
https://raw.githubusercontent.com/Lychee-Technology/duckdb-static/<commit>/test/fixtures/public
```

`<commit>` is the full SHA of a commit on `main` that contains the fixture version the test reads. The files at a SHA never change, and since a committed version never changes either, any later commit serves v1 identically. Don't put a branch name in place of the SHA: the URL would follow new commits, and GitHub caches raw files for five minutes (`Cache-Control: max-age=300`), so it can serve old files right after a push.

Reading needs no credentials. GitHub rate-limits unauthenticated requests to `raw.githubusercontent.com`, and a `verify` run makes 22 of them. A `429` status means the limit was hit, not that the bundle or the fixtures are wrong. GitHub says authenticated requests get higher limits, so `test/smoke.sh` sends `GITHUB_TOKEN` when it is set, but only to `https://raw.githubusercontent.com/Lychee-Technology/duckdb-static/`. `verify` always reads without it.

Cloudflare Pages was tried first and doesn't work. It answers a ranged `GET` with `200` and the whole file, and its `HEAD` responses have no `Content-Length`, so httpfs can't read Parquet from it.

## Verifying the host

```bash
python3 test/fixtures/fixtures.py verify \
  https://raw.githubusercontent.com/Lychee-Technology/duckdb-static/$(git rev-parse origin/main)/test/fixtures/public
```

`verify` reads the base URL from `DUCKDB_TEST_HTTPFS_BASE_URL` when none is given. Against `<base URL>/v1/`, or another version's directory with `--version`, it checks that:

- `manifest.json` is byte-identical to the committed copy, so the URL points at a commit with the same fixtures;
- for each file, `HEAD` returns 200 and the exact `Content-Length`, and `GET` returns the committed bytes;
- ranged `GET`s of the first 4 bytes, the last 8 (the Parquet footer length and magic number, which httpfs reads first) and 4 KiB from the middle return `206`, an exact `Content-Range`, and the right bytes;
- no Parquet response has a `Content-Encoding`, even though those requests offer gzip and brotli, because compression breaks byte ranges;
- the `absent` file returns 404, rather than a fallback page with status 200.

A missing `Accept-Ranges: bytes` header is printed as a warning, not counted as a failure. Each failure names the request and what was wrong. A failure in the network rather than in the response is labeled `DNS:`, `TLS:`, `timeout:` or `connection:`, and a malformed or truncated response `protocol:`. The exit status is 0 if every check passed, 1 if any failed, 2 if no base URL was given, and 4 if the base URL isn't an `http://` or `https://` URL. Exit statuses 2 and 4 mean nothing was verified; neither is a pass.
