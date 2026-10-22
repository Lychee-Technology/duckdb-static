# Parquet fixtures

Small Parquet files with known contents, for testing httpfs reads of a real HTTPS endpoint. The same bytes are committed here and served from a public Cloudflare Pages site, so a test can run the same queries against a local server and against the site. A failure on the site alone points at the host or the network, and a failure on both points at the bundle.

| Path | What it is |
|---|---|
| `public/` | What the site serves. `public/v1/` holds the v1 files and `manifest.json`. |
| `v1.sql` | Writes the v1 files. |
| `v1.queries.sql` | The queries and the exact result each must give. |
| `fixtures.py` | Generates, checks and verifies the files. Python 3.9 or later, standard library only. |
| `test_fixtures.py` | Tests `fixtures.py verify` against a local server. |

## The v1 contract

| File | Rows | Row groups | Covers |
|---|---|---|---|
| `basic.parquet` | 100 | 1 | INTEGER, VARCHAR, DECIMAL, BOOLEAN, DATE |
| `nullable.parquet` | 1000 | 1 | NULLs in some rows and in every row |
| `nested.parquet` | 100 | 1 | lists with NULL elements, empty and NULL lists, structs, NULL structs, maps |
| `multi_row_group.parquet` | 50000 | 5 | predicates and projections that skip row groups and column chunks |

`manifest.json` records each file's size, sha256, row count, row groups and schema, and the queries from `v1.queries.sql`. Each query returns one row and one column, and its `expected` value is that column cast to VARCHAR, which is what `duckdb_value_varchar` returns. In each query, replace `{dir}` with the directory that holds the files, such as `https://<site>/v1`. `absent` names a file that doesn't exist, which the site must answer with 404.

The manifest's hashes are the contract. Once a version is committed and deployed, its files never change, so a test pinned to `v1` keeps meaning the same thing. New or different data, or a different DuckDB, makes a new version (`v2.sql`, `v2.queries.sql` and a `VERSIONS` entry in `fixtures.py`) alongside the old one. `fixtures.py generate` refuses to overwrite an existing version.

## Generating and checking

`fixtures.py` writes the files with the official DuckDB CLI pinned for each version in `VERSIONS`: v1 uses DuckDB v2.0.0, independent of `duckdb_version`. It downloads the CLI for the host's architecture (x86_64 or aarch64) and checks its sha256. To avoid the download, set `DUCKDB_CLI` to a CLI that reports the same version.

```bash
python3 test/fixtures/fixtures.py check v1      # regenerate and compare with public/v1
python3 test/fixtures/fixtures.py generate v2   # write a new version
python3 test/fixtures/test_fixtures.py
```

Generation runs single-threaded, with extension autoloading off, so the bytes don't depend on the machine or the network. It checks every query in `<version>.queries.sql` against the new files and fails with the expected and actual values if one differs. `.github/workflows/fixtures.yml` runs `check v1` and the tests on native arm64 and x86_64 runners.

## Hosting on Cloudflare Pages

The site serves the contents of `test/fixtures/public`. Reading it needs no credentials. Either:

- **Connect the repository.** Create a Pages project from this GitHub repository, with no framework preset and no build command, and with `test/fixtures/public` as the build output directory. Production deploys follow `main`.
- **Upload directly.** Run `npx wrangler pages deploy test/fixtures/public --project-name <project>` after logging in with `npx wrangler login`.

The site is then at `https://<project>.pages.dev`, or a custom domain if you add one. That URL, without a version, is the base URL. Deploy the whole `public` directory every time, so older versions stay available.

Pages limits a file to 25 MiB. The v1 files total under 0.5 MB.

## Verifying a deployment

```bash
python3 test/fixtures/fixtures.py verify https://<project>.pages.dev
```

`verify` reads the base URL from `DUCKDB_TEST_HTTPFS_BASE_URL` when none is given. Against `<base URL>/v1/` it checks that:

- `manifest.json` is byte-identical to the committed copy, so the site isn't serving a stale deployment;
- for each file, `HEAD` returns 200 and the exact `Content-Length`, and `GET` returns the committed bytes;
- ranged `GET`s of the first 4 bytes, the last 8 (the Parquet footer length and magic number, which httpfs reads first) and 4 KiB from the middle return `206`, an exact `Content-Range`, and the right bytes;
- no response has a `Content-Encoding`, even though the requests offer gzip and brotli, because compression breaks byte ranges;
- the `absent` file returns 404, rather than a fallback page with status 200.

A missing `Accept-Ranges: bytes` header is printed as a warning, not counted as a failure. Each failure names the request and what was wrong. A failure in the network rather than in the response is labeled `DNS:`, `TLS:`, `timeout:` or `connection:`. The exit status is 0 if every check passed, 1 if any failed, and 2 if no base URL was given. Exit status 2 means nothing was verified; it is not a pass.

If Pages fails the range or encoding checks, try an R2 public bucket on a custom domain before weakening the tests. Don't use an `r2.dev` URL, which is rate-limited. Upload the same `public` tree, and the base URL changes but nothing else does.
