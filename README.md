# DuckDB Static Library

> A precompiled static bundle of DuckDB optimized for serverless environments (AWS Lambda, Google Cloud Functions, etc.).


## Why DuckDB Static Library?

In serverless environments, [DuckDB](https://duckdb.org/) adds initialization overhead to every cold start, and its extensions have to be managed as separate dependencies. This project bundles DuckDB and its extensions into one static library.

### Fast Cold Starts
Loading extensions dynamically can push initialization past 4s. The static bundle has everything precompiled, and initialization takes about 0.3s.

### Batteries Included
Using DuckDB with extensions in ephemeral environments often leads to runtime errors due to missing binaries or version mismatches. The extensions are compiled into the bundle, so nothing is downloaded at runtime. DuckDB and the extensions come from one build, so their versions always match.


## What's Included?

The bundle contains DuckDB 2.0.0 (the version in [`duckdb_version`](duckdb_version)), is built on Amazon Linux 2023, and includes these extensions:

| Extension        | Description                                                |
| :--------------- | :--------------------------------------------------------- |
| `core_functions` | DuckDB's standard function library, an extension since 2.0 |
| `parquet`        | Parquet columnar file format support                       |
| `json`           | JSON manipulation and querying                             |
| `icu`            | Time zones, calendars and collations                       |
| `httpfs`         | HTTP file system support (S3, GCS, etc.)                   |


## How It Works

### Automated Build Process
A GitHub Actions workflow builds and publishes the bundle:
1.  **Trigger:** Pushing a `v*` tag starts the workflow.
2.  **Build:** Compiles DuckDB and its extensions on Amazon Linux 2023, once for arm64 and once for amd64, and merges them into one static library with [`scripts/package-bundle.sh`](scripts/package-bundle.sh).
3.  **Smoke test:** Checks that each tarball holds exactly `libduckdb_bundle.a`, `duckdb.h` and `duckdb_static_extension.h`, built for the runner's architecture. Then links [`test/smoke.c`](test/smoke.c) against the bundle and the shipped headers and checks the result of a query for every extension, including httpfs reads over HTTP and S3 from a local server. It also checks core SQL, nested JSON, local Parquet and a reopened database file, the queries of the [Parquet fixtures](test/fixtures/README.md) read through httpfs from the local server, and that the extensions are missing when nothing registers them. It fails if a check fails, if none ran, or if a test binary needs a shared library other than the ones listed under [Linking and registering the extensions](#linking-and-registering-the-extensions).
4.  **Release:** Checks each `.tar.xz` file against the sha256 its build recorded after the smoke test, and publishes the files and a `SHA256SUMS` to GitHub Releases.
5.  **Test:** Alongside the release, builds the Lambda in [`example/`](example/) against each new bundle and invokes it with SAM, following `example/README.md`, and runs the fixture queries over HTTPS. The release waits for neither; see [Releasing](#releasing).

### Installation / Usage
You don't need to build from source:
1.  Go to the [Releases](https://github.com/Lychee-Technology/duckdb-static/releases) page.
2.  Download the latest `.tar.xz` file for your architecture. To check it, download `SHA256SUMS` next to it and run `sha256sum -c --ignore-missing SHA256SUMS`.
3.  Link the `libduckdb_bundle.a` inside it into your function at build time, and register the extensions as described below. [`example/`](example/) does this for a Go Lambda.

The bundle is compiled for `x86-64-v3` on amd64 (Intel Haswell, AMD Zen, or newer) and for `armv8.2-a+crypto+fp16+dotprod+lse` on arm64 (AWS Graviton2 or newer). On older CPUs it crashes with an illegal-instruction error. Both AWS Lambda architectures meet these requirements.

### Linking and registering the extensions
The tarball contains `libduckdb_bundle.a` and the headers `duckdb.h` and `duckdb_static_extension.h`. Link the bundle with:

```
-L<dir> -lduckdb_bundle -lstdc++ -lm -lcurl -lssl -lcrypto -lpthread -ldl
```

DuckDB itself is linked statically. libstdc++, libcurl and OpenSSL are linked dynamically from the system. A binary linked this way needs these shared libraries at run time: libstdc++, libm, libcurl, libssl, libcrypto, libgcc_s, libc and the dynamic loader, plus libcurl's own dependencies, which on Amazon Linux 2023 include nghttp2, libpsl and Kerberos and are installed with it. It doesn't need a DuckDB shared library. On Amazon Linux 2023, link with `gcc14-gcc` or `gcc14-g++` (and `gcc14-libstdc++-devel`), the compiler the bundle is built with.

Since DuckDB 2.0, a statically linked extension does nothing until the program registers it, and the registration has to happen before the first database opens. The bundle contains DuckDB's generated loader for its extensions, so either of these works:

- Call `duckdb_register_static_extensions()` before opening a database. It returns 0 on success. No DuckDB header declares it, so declare it yourself: `int32_t duckdb_register_static_extensions(void);`
- Link with `-Wl,-u,duckdb_register_static_extensions`. That pulls the loader into the binary, and it registers the extensions before `main`.

`duckdb-go` `v2.20000.0` and later call `duckdb_register_static_extensions()` themselves, so a Go program like [`example/`](example/) needs neither.

Doing both is harmless, because registering an extension twice does nothing. If neither is done, DuckDB still opens, but the bundled extensions are missing. For example, `SELECT to_minutes(5)` fails with an error naming `core_functions`. DuckDB loads the extensions in the order `core_functions`, `parquet`, `json`, `icu`, `httpfs`, so `LOAD parquet` in your code is a no-op.

The bundle can't install or load any other extension. It is built without DuckDB's loadable-extension support, so `INSTALL` and `LOAD` of an extension that isn't bundled fail, and nothing is downloaded at runtime.

### Upgrading from a DuckDB 1.x bundle
The release names and the single `libduckdb_bundle.a` stay the same. What changes for a consumer:

- **Registration:** Bundles up to 1.5.x loaded their extensions without being asked. A 2.0 bundle needs one of the two steps above.
- **Storage format:** A database file that DuckDB 2.0 creates uses storage version 69, which DuckDB before 2.0 can't read. Files created by older versions stay readable and keep their format. If older readers share your database files, set `storage_compatibility_version` or create the files with `ATTACH ... (STORAGE_VERSION 'v1.5.6')`. In-memory databases and Parquet files aren't affected.
- **SQL changes:** 2.0 has a new parser, and changes some semantics, such as integer division by zero now raising an error. The `legacy_parser` extension that restores the old parser has to be installed, so it can't be used with this bundle.

See DuckDB's [migration guide](https://duckdb.org/docs/2.0/operations_manual/migration) and [Statically Linking Extensions](https://duckdb.org/docs/2.0/dev/building/linking_extensions).

### Building and testing
[`.github/workflows/build-and-release.yml`](.github/workflows/build-and-release.yml) is the build recipe. Its steps run in an `amazonlinux:2023` container. To build locally, run them in that container on a host of the same architecture. After `make release`, package the bundle and smoke-test it with the same environment as the workflow:

```bash
CXX=gcc14-g++ CXXFLAGS="-march=x86-64-v3 -D_GLIBCXX_USE_CXX11_ABI=1 -ffunction-sections -fdata-sections" \
  scripts/package-bundle.sh duckdb 'core_functions;parquet;json;icu;httpfs' libduckdb_bundle-amd64-linux-httpfs-parquet.tar.xz
CC=gcc14-gcc test/smoke.sh libduckdb_bundle-amd64-linux-httpfs-parquet.tar.xz v2.0.0
```

On arm64, use the arm64 `CXXFLAGS` from the workflow's matrix and the `arm64` file name. To build and invoke the Lambda example against a local bundle, see [`example/README.md`](example/README.md). The Parquet fixtures for testing httpfs against a public HTTPS site, and how to host and verify them, are described in [`test/fixtures/README.md`](test/fixtures/README.md).

To also run the fixture queries over real HTTPS, point `DUCKDB_TEST_HTTPFS_BASE_URL` at the fixtures on GitHub, pinned to a commit on `main`:

```bash
DUCKDB_TEST_HTTPFS_BASE_URL=https://raw.githubusercontent.com/Lychee-Technology/duckdb-static/$(git rev-parse origin/main)/test/fixtures/public \
  CC=gcc14-gcc test/smoke.sh libduckdb_bundle-amd64-linux-httpfs-parquet.tar.xz v2.0.0
```

After the local checks, `test/fixtures/fixtures.py verify` checks that the host serves the committed files with working byte ranges, and then the same queries run against it. With `GITHUB_TOKEN` set, requests to this repository's files on `raw.githubusercontent.com` send it as a bearer token, because GitHub limits anonymous downloads more tightly; it is never sent to another URL or printed. Without `DUCKDB_TEST_HTTPFS_BASE_URL`, the summary at the end says the remote run is not configured, which is not a pass.

The fixture checks set httpfs's retries (`http_retries`, `http_retry_wait_ms`, `http_timeout`) rather than re-running a failed query, so a wrong result can't be retried into a pass. On a `429` or `503`, httpfs adds up to five retries and honors `Retry-After` up to 20 s. Against GitHub, a run without retries makes 25 requests for the queries, plus 22 for the preflight.

In CI, each build job writes its tarball's sha256 to the log, the job summary and a `.sha256` file in its artifact. When the repository variable `DUCKDB_TEST_HTTPFS_BASE_URL` is set, the `httpfs-remote` job then runs `test/smoke.sh` with it on each architecture's native runner, in a fresh Amazon Linux 2023 container, after checking the downloaded tarball against that sha256. Only its smoke step gets the workflow's token as `GITHUB_TOKEN`. It refuses a URL on a GitHub host unless it has the form `https://raw.githubusercontent.com/<owner>/<repo>/<full commit SHA>/…`, because `http://`, another spelling of the host and `github.com`'s `/raw/` links all reach a branch's cached files too. It copies the smoke test's summary to its job summary, with a separate warning when the host answered `429`. The release doesn't wait for it. If the variable is unset when the run starts, the job shows as skipped; a change to the variable doesn't reach runs that have already started. To set it:

```bash
gh variable set DUCKDB_TEST_HTTPFS_BASE_URL \
  --body https://raw.githubusercontent.com/Lychee-Technology/duckdb-static/<commit>/test/fixtures/public
```

`test/smoke.sh` exits 0 when every check that ran passed, 3 when only the remote run failed and every failure was in the host or the network, and 1 otherwise: a failure that points at the bundle, or a `DUCKDB_TEST_HTTPFS_BASE_URL` or `GITHUB_TOKEN` that can't be used.

When the smoke test fails, a line starting with `FAIL` says what went wrong:

- `is not a readable .tar.xz` or `the tarball holds`: the tarball is damaged, or `scripts/package-bundle.sh` packaged the wrong files.
- `libduckdb_bundle.a is built for`: the tarball is for the other architecture, or the archive mixes objects from two.
- `doesn't build against the tarball's headers and archive`: a shipped header doesn't compile, or the archive lacks a symbol a consumer links, such as an extension's `describe` function. The compiler's error above it names the cause.
- `needs <library>, which isn't on smoke.sh's allowlist`: consumers would need a new shared library at run time. If that is intended, add the library to the allowlist in `test/smoke.sh` and to the list above.
- `FAIL <query>`: DuckDB returned a wrong value or error. `<n> of <m> checks failed` follows. An error from the network or an HTTP server is labeled `[network: DNS]`, `[network: TLS]`, `[network: timeout]`, `[network: connection]`, `[host: HTTP <status>]` or `[host: byte ranges]`. After such an error in a fixture check, the remaining fixture checks are skipped. `[host: HTTP 429, rate-limited]` means GitHub's rate limit was hit after httpfs's retries; the bundle isn't at fault. The summary then ends with a `rate-limited:` line, which also appears when the preflight got a `429`.
- `did not report any passed checks`: a binary exited without running its checks. If it crashed, its output stops after the last check that ran, with no `FAIL` line.
- `the server logged no ... request`: an httpfs check passed without reading from the local server.
- `read the fixtures without the token` or `sent the fixtures' token outside its scope`: httpfs didn't apply an `http` secret's `BEARER_TOKEN` to exactly the URLs under its `SCOPE`, which the remote run relies on to keep `GITHUB_TOKEN` away from other hosts.
- `remote: failed the preflight`: the host doesn't serve the fixtures with working byte ranges, or the base URL points at the wrong place, such as a commit without the fixtures. `fixtures.py verify`'s `FAIL` lines above say which request failed.
- `remote: not run: DUCKDB_TEST_HTTPFS_BASE_URL isn't an http:// or https:// URL`: the variable is mistyped. This exits 1, not 3, so that automation doesn't take it for a host failure.

### Releasing
A run for a `v*` tag publishes its release only if the build job passed on both architectures, smoke test included. Before uploading, the release job checks each tarball against the sha256 its build job recorded right after the smoke test, and fails if one differs or is missing. It publishes those records as `SHA256SUMS`, whose lines match the `Smoke-tested:` line in each build job's summary. Nothing is rebuilt between the test and the upload.

Two jobs run alongside the release, and the release waits for neither:

- `example` builds and invokes the Go Lambda. A DuckDB release can ship before a `duckdb-go` version that matches it, so a failed example doesn't hold back the bundle.
- `httpfs-remote` runs the fixture queries over HTTPS. It stays a separate signal until the fixture hosting has proven reliable; see below.

A dispatch on a tag that already has a release, including a draft, fails in the release job and leaves the release as it is. To replace its assets, dispatch with `overwrite-release`:

```bash
gh workflow run build-and-release.yml --ref <tag> -f overwrite-release=true
```

Without the input, the dispatch still builds and tests the tag before the release job fails, so it can retest a release without changing it. GitHub runs the workflow file at the dispatched ref, so tags up to `v1.5.6_0`, which predate this check, still overwrite their release on dispatch. A tag push isn't checked: it fills a release that someone created for the tag before pushing it. Prefer a new `_<n>` tag over overwriting, so that a published release's assets don't change under its users.

#### The HTTPS check
How to read `httpfs-remote`:

- **Skipped:** `DUCKDB_TEST_HTTPFS_BASE_URL` was unset when the run started. Nothing was tested over HTTPS, which isn't a pass.
- **Passed:** every fixture query gave its expected result over HTTPS, on the tarball whose sha256 the job summary names.
- **Failed with `Host or network failure`** (exit 3): GitHub or the network failed, and no failed check points at the bundle. A `Rate-limited` warning means GitHub answered `429`. This says nothing against the release. Re-run the failed jobs to get a result while the run's artifacts are kept, which is one day.
- **Failed with `Bundle or configuration failure`** (exit 1): a fixture query gave a wrong result over HTTPS, or the base URL or token can't be used. The same queries passed against the local server in the build job, so a wrong result points at something only real HTTPS exercises, such as TLS. Treat it as a bundle bug. A release that is already published keeps its assets, and the fix ships under the next `_<n>` tag.

Making `httpfs-remote` required is a separate decision, recorded on an issue. It needs evidence that GitHub serves the fixtures reliably: the job's summaries from release and dispatch runs on both architectures, including how often they show host failures or `Rate-limited`. The change that makes it required has to:

- add `httpfs-remote` to the release job's `needs`;
- make the job fail, not skip, when the variable is unset. A job that needs a skipped job is skipped too, so an unset variable would skip the release with no error saying why;
- say what an outage does. A required job's host failure holds the release until a re-run passes; after the artifacts expire, dispatch on the tag to build it again.


## Contributing
Pull requests are welcome.


## Disclaimer

This project is an independent open-source initiative and is not affiliated with, endorsed by, or associated with the DuckDB Foundation or DuckDB Labs.
DuckDB is a trademark of the DuckDB Foundation. All other trademarks, logos, and service marks used in this repository are the property of their respective owners.
