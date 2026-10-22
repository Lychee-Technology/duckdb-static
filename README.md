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
3.  **Smoke test:** Links [`test/smoke.c`](test/smoke.c) against each bundle and checks the result of a query for every extension, including httpfs reads over HTTP and S3 from a local server. It also checks that the extensions are missing when nothing registers them.
4.  **Release:** Packages each build as a `.tar.xz` file and publishes it to GitHub Releases.
5.  **Test:** Alongside the release, builds the Lambda in [`example/`](example/) against each new bundle and invokes it with SAM, following `example/README.md`.

### Installation / Usage
You don't need to build from source:
1.  Go to the [Releases](https://github.com/Lychee-Technology/duckdb-static/releases) page.
2.  Download the latest `.tar.xz` file for your architecture.
3.  Link the `libduckdb_bundle.a` inside it into your function at build time, and register the extensions as described below. [`example/`](example/) does this for a Go Lambda.

The bundle is compiled for `x86-64-v3` on amd64 (Intel Haswell, AMD Zen, or newer) and for `armv8.2-a+crypto+fp16+dotprod+lse` on arm64 (AWS Graviton2 or newer). On older CPUs it crashes with an illegal-instruction error. Both AWS Lambda architectures meet these requirements.

### Linking and registering the extensions
The tarball contains `libduckdb_bundle.a` and the headers `duckdb.h` and `duckdb_static_extension.h`. Link the bundle with:

```
-L<dir> -lduckdb_bundle -lstdc++ -lm -lcurl -lssl -lcrypto -lpthread -ldl
```

DuckDB itself is linked statically. libstdc++, libcurl and OpenSSL are linked dynamically from the system. On Amazon Linux 2023, link with `gcc14-gcc` or `gcc14-g++` (and `gcc14-libstdc++-devel`), the compiler the bundle is built with.

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

On arm64, use the arm64 `CXXFLAGS` from the workflow's matrix and the `arm64` file name. To build and invoke the Lambda example against a local bundle, see [`example/README.md`](example/README.md).


## Contributing
Pull requests are welcome.


## Disclaimer

This project is an independent open-source initiative and is not affiliated with, endorsed by, or associated with the DuckDB Foundation or DuckDB Labs.
DuckDB is a trademark of the DuckDB Foundation. All other trademarks, logos, and service marks used in this repository are the property of their respective owners.
