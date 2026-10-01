# DuckDB Static Library

> A precompiled static bundle of DuckDB optimized for serverless environments (AWS Lambda, Google Cloud Functions, etc.).


## Why DuckDB Static Library?

In serverless environments, [DuckDB](https://duckdb.org/) adds initialization overhead to every cold start, and its extensions have to be managed as separate dependencies. This project bundles DuckDB and its extensions into one static library.

### Fast Cold Starts
Loading extensions dynamically can push initialization past 4s. The static bundle has everything precompiled, and initialization takes about 0.3s.

### Batteries Included
Using DuckDB with extensions in ephemeral environments often leads to runtime errors due to missing binaries or version mismatches. The extensions are compiled into the bundle, so nothing is downloaded at runtime. DuckDB and the extensions come from one build, so their versions always match.


## What's Included?

The bundle is built on Amazon Linux 2023 and includes these extensions:

| Extension | Description                                                |
| :-------- | :--------------------------------------------------------- |
| `json`    | JSON manipulation and querying                             |
| `icu`     | International Components for Unicode                       |
| `httpfs`  | HTTP file system support (S3, GCS, etc.)                   |
| `parquet` | Parquet columnar file format support                       |


## How It Works

### Automated Build Process
A GitHub Actions workflow builds and publishes the bundle:
1.  **Trigger:** Pushing a `v*` tag starts the workflow.
2.  **Build:** Compiles DuckDB and its extensions into a static library on Amazon Linux 2023, once for arm64 and once for amd64.
3.  **Release:** Packages each build as a `.tar.xz` file and publishes it to GitHub Releases.

### Installation / Usage
You don't need to build from source:
1.  Go to the [Releases](https://github.com/Lychee-Technology/duckdb-static/releases) page.
2.  Download the latest `.tar.xz` file for your architecture.
3.  Link the `libduckdb_bundle.a` inside it into your function at build time. [`example/`](example/) does this for a Go Lambda.

The bundle is compiled for `x86-64-v3` on amd64 (Intel Haswell, AMD Zen, or newer) and for `armv8.2-a+crypto+fp16+dotprod+lse` on arm64 (AWS Graviton2 or newer). On older CPUs it crashes with an illegal-instruction error. Both AWS Lambda architectures meet these requirements.


## Contributing
Pull requests are welcome.


## Disclaimer

This project is an independent open-source initiative and is not affiliated with, endorsed by, or associated with the DuckDB Foundation or DuckDB Labs.
DuckDB is a trademark of the DuckDB Foundation. All other trademarks, logos, and service marks used in this repository are the property of their respective owners.
