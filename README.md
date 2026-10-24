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
1.  **Trigger:** workflow triggers automatically whenever a new tag is pushed.
2.  **Build:** Compiles a static bundle of DuckDB + Extensions on Amazon Linux 2023.
3.  **Smoke test:** Checks that each tarball holds exactly `libduckdb_bundle.a`, built for the runner's architecture. Then links [`test/smoke.c`](test/smoke.c) against it with the `duckdb.h` of the DuckDB checkout it was built from, and checks the result of a query for every extension, including httpfs reads over HTTP and S3 from a local server, and that installing or loading any other extension fails. It also checks core SQL, nested JSON, local Parquet and a reopened database file, and that those checks left no other extension installed or loaded. It fails if a check fails, if none ran, or if the test binary needs a shared library other than the ones listed under [Linking](#linking). Only a tarball that passes is uploaded.
4.  **Release:** Automatically generates `.tar.xz` files and publishes them to GitHub Releases.

### Installation / Usage
You don't need to build from source. simply:
1.  Open the newest `v1.5.6_<n>` release, currently [**v1.5.6_0**](https://github.com/Lychee-Technology/duckdb-static/releases/tag/v1.5.6_0). The release marked Latest is `main`'s DuckDB 2.0 build, and its tarballs have the same names, so choose the release by its tag.
2.  Download the `.tar.xz` file for the corresponding arch.
3.  Include the binary in your serverless deployment package.

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

On arm64, replace `amd64` with `arm64`. Artifacts are kept for one day. For a released tarball, pass the directory of DuckDB v1.5.6's `src/include` instead. It ends with `<n> checks passed`. Otherwise, a line starting with `FAIL` says what went wrong:

- `has no duckdb.h`: the third argument isn't the directory holding the header.
- `is not a readable .tar.xz` or `the tarball holds`: the tarball is damaged, or the build packaged the wrong files.
- `libduckdb_bundle.a is built for`: the tarball is for the other architecture, or the archive mixes objects from two.
- `doesn't build against`: the header doesn't match the archive, or the archive lacks a symbol the program links, such as a dropped extension member. The compiler's error above it names the cause.
- `needs <library>, which isn't on smoke.sh's allowlist`: consumers would need a new shared library at run time. If that is intended, add the library to the allowlist in `test/smoke.sh` and to [Linking](#linking).
- `FAIL <query>`: DuckDB returned a wrong value or error. `<n> of <m> checks failed` follows.
- `did not report any passed checks`: the binary exited without running its checks. If it crashed, its output stops after the last check that ran, with no `FAIL` line.
- `the server logged no ... request`: an httpfs check passed without reading from the local server.

The Parquet fixtures for testing httpfs against a public HTTPS site, and how to regenerate, host and verify them, are described in [`test/fixtures/README.md`](test/fixtures/README.md). `.github/workflows/fixtures.yml` regenerates them on native arm64 and x86_64 runners when a PR or a push to this branch changes them.


## Contributing
Contributions are welcome! Please feel free to submit a Pull Request.


## Disclaimer

This project is an independent open-source initiative and is **not** affiliated with, endorsed by, or associated with the DuckDB Foundation or DuckDB Labs.
**DuckDB** is a trademark of the **DuckDB Foundation**. All other trademarks, logos, and service marks used in this repository are the property of their respective owners.