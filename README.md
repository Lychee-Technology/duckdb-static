# DuckDB Static Library

> **A pre-compiled, static bundle of DuckDB optimized for serverless environments (AWS Lambda, Google Cloud Functions, etc.).**

This is the `duckdb-v1.5.x` maintenance branch, which builds DuckDB 1.5.6. `main` builds DuckDB 2.0. [docs/backport-1.5.x.md](docs/backport-1.5.x.md) lists how the two differ and what this branch takes from `main`.


## Why DuckDB Static Library?

Running [DuckDB](https://duckdb.org/) in serverless environments can be challenging due to initialization overhead and dependency management. This project solves these pain points by providing a strictly bundled static library.

### ️ Blazing Fast Cold Starts
**Cold start time is critical.**
When using dynamic loading, initialization can be slow. By using a static bundle, all necessary components are pre-compiled into a single package.
* **Performance Gain:** Reduces initialization time from **>4s** down to **~0.3s**.
* **Result:** Significantly improved responsiveness for Lambda/Cloud Functions relying on DuckDB and its extensions.

### Robust & Portable ("Batteries Included")
Using DuckDB with extensions in ephemeral environments often leads to runtime errors due to missing binaries or version mismatches.
* **Zero Dependencies:** No need to download extensions at runtime.
* **Consistency:** Ensures that all core components are version-matched and guaranteed to work, simplifying deployment across different systems.


## What's Included?

This bundle is built on **Amazon Linux 2023** and comes pre-packaged with the most essential extensions:

| Extension | Description                                                |
| :-------- | :--------------------------------------------------------- |
| `core_functions` | DuckDB's core scalar and aggregate functions        |
| `json`    | JSON manipulation and querying                             |
| `icu`     | International Components for Unicode                       |
| `httpfs`  | HTTP file system support (S3, GCS, etc.)                   |
| `parquet` | Parquet columnar file format support                       |


## How It Works

### Automated Build Process
This repository utilizes a **GitHub Actions** workflow to ensure reliability and transparency:
1.  **Trigger:** a `v*` tag push builds and releases; see [Releasing](#releasing) for the tag names it accepts. A PR into `duckdb-v1.5.x`, or a push to it, that changes `duckdb_version`, `test/` or the workflow, other than in Markdown files, builds and tests its own bundles without releasing.
2.  **Build:** Compiles a static bundle of DuckDB + Extensions on Amazon Linux 2023.
3.  **Smoke test:** Checks that each tarball holds exactly `libduckdb_bundle.a`, built for the runner's architecture. Then links [`test/smoke.c`](test/smoke.c) against it with the `duckdb.h` of the DuckDB checkout it was built from, and checks the result of a query for every extension, including httpfs reads over HTTP and S3 from a local server, and that installing or loading any other extension fails. It also checks core SQL, nested JSON, local Parquet and a reopened database file, that those checks left no other extension installed or loaded, and the queries of the [Parquet fixtures](test/fixtures/README.md) read through httpfs from the local server. It fails if a check fails, if none ran, or if the test binary needs a shared library other than the ones listed under [Linking](#linking). Only a tarball that passes is uploaded, with the sha256 recorded right after the test.
4.  **Release:** Checks that each tarball is the one its smoke test passed, then publishes the two tarballs and a `SHA256SUMS`, without marking the release Latest. See [Releasing](#releasing).
5.  **Test over HTTPS:** Alongside the release, the `httpfs-remote` job runs the fixture queries on each new bundle against a public HTTPS copy of the fixtures. The release doesn't wait for it; see [The HTTPS check](#the-https-check).
6.  **Test the example:** Alongside the release, builds the Lambda in [`example/`](example/) against each new bundle and invokes it with SAM, following `example/README.md`. The release doesn't wait for it either.

### Installation / Usage
You don't need to build from source. simply:
1.  Open the newest `v1.5.6_<n>` release, currently [**v1.5.6_0**](https://github.com/Lychee-Technology/duckdb-static/releases/tag/v1.5.6_0). The release marked Latest is `main`'s DuckDB 2.0 build, and its tarballs have the same names, so choose the release by its tag.
2.  Download the `.tar.xz` file for the corresponding arch.
3.  From `v1.5.6_1` on, check it against the release's `SHA256SUMS` with `sha256sum -c --ignore-missing SHA256SUMS`.
4.  Include the binary in your serverless deployment package. [`example/`](example/) links it into a Go Lambda.

### Linking
The tarball contains only `libduckdb_bundle.a`. Compile against `duckdb.h` from DuckDB v1.5.6 (`src/include/duckdb.h`), and link with:

```
-L<dir> -lduckdb_bundle -lstdc++ -lm -lcurl -lssl -lcrypto -lpthread -ldl
```

DuckDB itself is linked statically. libstdc++, libcurl and OpenSSL are linked dynamically from the system. A binary linked this way needs these shared libraries at run time: libstdc++, libm, libcurl, libssl, libcrypto, libgcc_s, libc and the dynamic loader, plus libcurl's own dependencies, which on Amazon Linux 2023 are installed with it. It doesn't need a DuckDB shared library. On Amazon Linux 2023, link with `gcc14-gcc` or `gcc14-g++`, the compiler the bundle is built with.

Every database opened through the C API, which includes the Go bindings, has the five extensions loaded; nothing has to register them. The same holds from C++ with the default `DBConfig`. Only a C++ caller that sets `DBConfigOptions::load_extensions` to `false` opens a database without them. `INSTALL` and `LOAD` of any other extension fail with a Permission Error, because the bundle can't download extensions.

### Testing a bundle
[`test/smoke.sh`](test/smoke.sh) takes a tarball, the DuckDB version it must report, and a directory holding the matching `duckdb.h`. Each build uploads that header as the `duckdb-header-<arch>` artifact next to the tarball, so a CI build can be tested without a DuckDB checkout. On an x86_64 machine, in a fresh Amazon Linux 2023 container:

```bash
bundle_dir=$(mktemp -d)
gh run download <run id> -n libduckdb-amd64-httpfs-parquet -n duckdb-header-amd64 -D "$bundle_dir"
docker run --rm -v "$PWD":/repo:ro -v "$bundle_dir":/bundle:ro -w /repo \
  public.ecr.aws/amazonlinux/amazonlinux:2023 bash -c '
    dnf install -y gcc14 gcc14-c++ libcurl-devel openssl-devel binutils findutils tar xz python3 &&
    . ./duckdb_version &&
    CC=gcc14-gcc test/smoke.sh /bundle/libduckdb-amd64-httpfs-parquet/libduckdb_bundle-amd64-linux-httpfs-parquet.tar.xz "$DUCKDB_VER" /bundle/duckdb-header-amd64'
```

On arm64, replace `amd64` with `arm64`. Artifacts are kept for one day. The tarball's artifact also holds `<tarball>.sha256`, recorded after the build's smoke test, which `sha256sum -c` checks. For a released tarball, pass the directory of DuckDB v1.5.6's `src/include` instead.

To also run the fixture queries over HTTPS, pass `-e DUCKDB_TEST_HTTPFS_BASE_URL=<base URL>` to `docker run`, with a base URL as described in [`test/fixtures/README.md`](test/fixtures/README.md#hosting). The script first checks the host with `fixtures.py verify`, then runs the queries against it. Unset, the remote run is reported as not configured, which isn't a pass. `GITHUB_TOKEN`, when set, is sent only to this repository's files on `raw.githubusercontent.com` and never printed.

The binary's output ends with `<n> checks passed`, and the script's with a summary: `local:` and `remote:` lines, plus a `rate-limited:` line when the host answered 429. It exits 0 if everything that ran passed, 3 if only the remote run failed and every failure was in the host or the network, and 1 otherwise. A line starting with `FAIL` says what went wrong:

- `has no duckdb.h`: the third argument isn't the directory holding the header.
- `is not a readable .tar.xz` or `the tarball holds`: the tarball is damaged, or the build packaged the wrong files.
- `libduckdb_bundle.a is built for`: the tarball is for the other architecture, or the archive mixes objects from two.
- `doesn't build against`: the header doesn't match the archive, or the archive lacks a symbol the program links, such as a dropped extension member. The compiler's error above it names the cause.
- `needs <library>, which isn't on smoke.sh's allowlist`: consumers would need a new shared library at run time. If that is intended, add the library to the allowlist in `test/smoke.sh` and to [Linking](#linking).
- `FAIL <query>`: DuckDB returned a wrong value or error. `<n> of <m> checks failed` follows. An error labeled `[host: ...]` or `[network: ...]` came from the HTTP server or the network: an HTTP error status, a host that ignores byte ranges, or a DNS, TLS, connection or timeout failure. In the remote run, the remaining fixture checks then stop.
- `did not report any passed checks`: the binary exited without running its checks. If it crashed, its output stops after the last check that ran, with no `FAIL` line.
- `the server logged no ... request`: an httpfs check passed without reading from the local server, or the fixtures' bearer token wasn't sent where it should have been.
- `read the fixtures without the token scoped to them` or `sent the fixtures' token outside its scope`: the `TYPE http` secret's `SCOPE` didn't limit the token to the fixtures. The request lines above it are the ones at fault.
- `GITHUB_TOKEN has a quote, a space or a control character`: the remote run didn't start, because the token can't go into an SQL string and an HTTP header.
- Lines from `fixtures.py verify` before `== smoke --remote`: the host doesn't serve the committed fixtures with working byte ranges, so no query ran against it. [`test/fixtures/README.md`](test/fixtures/README.md#verifying-the-host) explains them.

The Parquet fixtures for testing httpfs against a public HTTPS site, and how to regenerate, host and verify them, are described in [`test/fixtures/README.md`](test/fixtures/README.md). `.github/workflows/fixtures.yml` regenerates them on native arm64 and x86_64 runners when a PR or a push to this branch changes them.

To build and invoke the Lambda example against a bundle you have, such as a CI artifact, see [`example/README.md`](example/README.md).

### Releasing
A run for a tag publishes its release only if the build job passed on both architectures, smoke test included. Before uploading, the release job checks each tarball against the sha256 its build job recorded right after the smoke test, and fails if one differs or is missing. It publishes those records as `SHA256SUMS`, whose lines match the `Smoke-tested:` line in each build job's summary. Nothing is rebuilt between the test and the upload. `httpfs-remote` and `example` run alongside the release, which waits for neither. For `httpfs-remote`, see [The HTTPS check](#the-https-check). `example` builds and invokes the Go Lambda; a DuckDB release can ship before a `duckdb-go` version that matches it, so a failed example doesn't hold back the bundle.

A release from this branch isn't marked Latest. Latest stays `main`'s newest DuckDB 2.0 release, whose tarballs have the same names, and which `main`'s example downloads as `releases/latest`. This branch's example downloads the newest `v1.5.6_<n>` release instead.

Tag a commit of this branch as `v<DuckDB version>_<n>`, with the DuckDB version `duckdb_version` sets, such as `v1.5.6_1`. The build fails before compiling if the tag names another version, or if `duckdb_version` isn't a DuckDB 1.5.x version, so this branch's workflow can't publish a 2.0 bundle or a bundle under another version's tag. It also fails on `v1.5.6_0` and earlier tags, which earlier workflows released: a tag push isn't checked for an existing release (below), so re-pushing one of them would replace its assets. GitHub runs the workflow file of the commit a tag points at: a `v*` tag on a commit of `main` runs `main`'s workflow and releases a DuckDB 2.0 bundle, which no check on this branch can stop ([#52](https://github.com/Lychee-Technology/duckdb-static/issues/52)). Keep this line's tags on this branch's commits.

A dispatch on a tag that already has a release, including a draft, fails in the release job and leaves the release as it is. To replace its assets, dispatch with `overwrite-release`:

```bash
gh workflow run build-and-release.yml --ref refs/tags/<tag> -f overwrite-release=true
```

Without the input, the dispatch still builds and tests the tag before the release job fails, so it can retest a release without changing it. Release jobs for the same tag run one at a time, so of two dispatches on a tag without a release, the second fails once the first has released. GitHub runs the workflow file at the dispatched ref, so tags up to `v1.5.6_0`, which predate this check, still overwrite their release on dispatch. A tag push isn't checked: it fills a release that someone created for the tag before pushing it. Prefer a new `_<n>` tag over overwriting, so that a published release's assets don't change under its users.

A dispatch on a branch, `duckdb-v1.5.x` or another, builds and tests without releasing.

### The HTTPS check
The `httpfs-remote` job runs `test/smoke.sh` on each architecture's tarball and header from the same run, in a fresh Amazon Linux 2023 container, with `DUCKDB_TEST_HTTPFS_BASE_URL` set to the repository variable of that name. The URL must be `https://`, because the job is the only check of TLS, and a URL on a GitHub host must also pin a full commit SHA on `raw.githubusercontent.com`, or the job fails before testing. How to read it:

- **Skipped:** the variable was unset when the run started. Nothing was tested over HTTPS, which isn't a pass.
- **Passed:** every fixture query gave its expected result over HTTPS, on the tarball whose sha256 the job summary names.
- **Failed with `Host or network failure`** (exit 3): GitHub or the network failed, and no failed check points at the bundle. A `Rate-limited` warning means GitHub answered `429`. This says nothing against the release. Re-run the failed jobs to get a result while the run's artifacts are kept, which is one day.
- **Failed with `Bundle or configuration failure`** (exit 1): a fixture query gave a wrong result over HTTPS, or the base URL or token can't be used. The same queries passed against the local server in the build job, so a wrong result points at something only real HTTPS exercises, such as TLS. Treat it as a bundle bug.

The release doesn't wait for `httpfs-remote`, as `main` decided in [#28](https://github.com/Lychee-Technology/duckdb-static/issues/28), and this branch keeps that policy. A bundle that fails only over real HTTPS can therefore still be published, with the failure showing in its run. Making the job required would mean adding it to the release job's `needs`, and making it fail rather than skip when the variable is unset, because a job that needs a skipped job is skipped too.


## Contributing
Contributions are welcome! Please feel free to submit a Pull Request.


## Disclaimer

This project is an independent open-source initiative and is **not** affiliated with, endorsed by, or associated with the DuckDB Foundation or DuckDB Labs.
**DuckDB** is a trademark of the **DuckDB Foundation**. All other trademarks, logos, and service marks used in this repository are the property of their respective owners.