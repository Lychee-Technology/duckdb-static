# DuckDB static bundle example for AWS Lambda

This is a Go Lambda function for the `provided.al2023` runtime. It links `libduckdb_bundle.a` from this repository's [releases](https://github.com/Lychee-Technology/duckdb-static/releases) into its binary. The extensions are compiled in, so the function runs `LOAD 'parquet'` without `INSTALL` and downloads nothing at runtime. Each invocation runs one query over a parquet file that ships with the function and returns the result.

## Requirements

- Docker. Go and the C/C++ toolchain run inside the build container, so you don't need Go on the host.
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).
- `make`, plus `curl`, `jq` and `tar` with xz support for `make download-libs`.
- A host with the same architecture as the function. The commands below build and run only for the host's own architecture. Building for the other one would run under emulation, which this README doesn't cover.
- A CPU that supports `x86-64-v3` or `armv8.2-a`. `sam local invoke` runs the bundle on your CPU, and older CPUs crash with an illegal-instruction error. Lambda meets both requirements. See the [top-level README](../README.md#installation--usage).

## Build and run

On an arm64 host (the default):

```bash
cd example
make download-libs
sam build
sam local invoke DuckDBParquetFunction
```

On an x86_64 host:

```bash
cd example
ARCH=amd64 make download-libs
ARCH=amd64 sam build --parameter-overrides Architecture=x86_64
sam local invoke DuckDBParquetFunction --parameter-overrides Architecture=x86_64
```

The invocation logs how long `LOAD 'parquet'` took and `dataDir: /var/task/data`, then prints the result:

```
"avg(array_length(tokens)): 10.500000\n"
```

What each step does:

- `make download-libs` finds the latest release of this repository, downloads `libduckdb_bundle-<ARCH>-linux-httpfs-parquet.tar.xz`, and extracts it into `libs/parquet/`. It skips the download if `libs/parquet/url.txt` already names the same asset. Add `FORCE=1` to download again. To use a bundle you already have, such as one from a branch run of this repository's build workflow, set `BUNDLE_TARBALL` to its `.tar.xz` path and `make download-libs` extracts it instead of downloading.
- `sam build` runs the Makefile target `build-DuckDBParquetFunction`. With `BuildMethod: makefile`, SAM looks for a target named `build-<resource name>`, so renaming either one breaks the build. The target runs `docker build` and writes `bootstrap` and `data/sample.parquet` into `.aws-sam/build/DuckDBParquetFunction/`.
- `sam local invoke` runs the built function in the `provided.al2023` runtime image.

### Architecture is set in two places

| Setting | Where | Values | What it picks |
| :-- | :-- | :-- | :-- |
| `ARCH` | `Makefile` | `arm64` (default), `amd64` | The release asset, the Docker platform, and the Go target |
| `Architecture` | `template.yaml` parameter | `arm64` (default), `x86_64` | The Lambda architecture: the runtime image for `sam local invoke`, and what `sam deploy` creates |

Both must name the same architecture, and nothing checks that they do. If you set only `ARCH=amd64`, you get an amd64 binary in an arm64 function. `sam build` passes its environment to `make`, which is how `ARCH` reaches the Makefile. The built template still refers to the parameter, so `sam local invoke` and `sam deploy` need the override as well. `sam deploy --guided` prompts for `Architecture`. Enter `x86_64` there if you built with `ARCH=amd64`.

## Sample data

The query reads `data/sample.parquet`. You don't need to download or create it. The `data` stage of the `Dockerfile` generates it during `sam build` with the DuckDB CLI image [`duckdb/duckdb`](https://hub.docker.com/r/duckdb/duckdb). The data is synthetic. It has 1000 rows with an `id` and a `tokens` list of 1 to 20 short strings, which is why the expected average is exactly 10.5.

To query your own file, change the `COPY` statement in the `data` stage, or add `COPY data/ /data/` to the `exporter` stage to ship files from a local `data/` directory. Then update the query in `cmd/main.go`. `data/` and `*.parquet` are gitignored, so local data files stay out of git.

## How the static link works

- The build image is Amazon Linux 2023 with gcc14. The bundle is built with the same compiler, and `provided.al2023` runs on the same distribution.
- The build sets `CGO_ENABLED=1`, `CPPFLAGS=-DDUCKDB_STATIC_BUILD`, and `CGO_LDFLAGS="-L/src/libs/parquet -lduckdb_bundle -lstdc++ -lm -lcurl -lssl -lcrypto -lpthread -ldl"`. DuckDB is linked statically. libstdc++, libcurl and OpenSSL are still linked dynamically from the system.
- `go build -tags=duckdb_use_static_lib` makes `duckdb-go` link the local `libduckdb_bundle.a` instead of the prebuilt libraries from `duckdb-go-bindings`.
- The `duckdb-go` version in `go.mod` encodes the DuckDB version: `v2.10506.0` goes with DuckDB 1.5.6. `make download-libs` always fetches the latest release. If that release has a newer DuckDB than `go.mod`, update `github.com/duckdb/duckdb-go/v2` to the matching version.

## Files

```
cmd/main.go     Lambda handler: loads parquet, queries data/sample.parquet
Dockerfile      builder (Go build and static link), data (sample parquet), exporter stages
Makefile        download-libs, build-DuckDBParquetFunction (run by sam build), clean
template.yaml   SAM template: DuckDBParquetFunction and the Architecture parameter
libs/           created by make download-libs
.aws-sam/       created by sam build; make clean removes .aws-sam/build
```
