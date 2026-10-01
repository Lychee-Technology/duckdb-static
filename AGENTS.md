# AGENTS.md

## What this repo is

This repo holds no DuckDB source. It is a CI recipe plus an example. The GitHub Actions workflow (`.github/workflows/build-and-release.yml`) clones upstream DuckDB at a pinned tag, builds `libduckdb_bundle.a` (DuckDB plus statically linked extensions) inside an Amazon Linux 2023 container, and publishes it to GitHub Releases as `libduckdb_bundle-{arm64,amd64}-linux-httpfs-parquet.tar.xz`. The goal is fast cold starts on AWS Lambda (`provided.al2023`) with no extension downloads at runtime.

There are no tests or linters. The repo has three parts:

- `duckdb_version`: a shell-sourceable file (`DUCKDB_VER`, `DUCKDB_EXTENSION_CI_TOOLS_BRANCH`). CI `source`s it, so keep it in `KEY=value` form.
- `.github/workflows/build-and-release.yml`: the whole build.
- `example/`: a Go Lambda (AWS SAM) that consumes a released bundle.

## Release flow

- If you push a tag matching `v*`, CI builds both arches and creates a GitHub Release. `workflow_dispatch` only builds and uploads artifacts (1-day retention); it does not release.
- Tags follow `v<duckdb-version>_<n>`, for example `v1.5.5_0` and `v1.5.4_1`. `<n>` is a rebuild counter for the same DuckDB version.
- To bump DuckDB, edit both values in `duckdb_version`. The example's `duckdb-go` versions encode the DuckDB version: `duckdb-go/v2 v2.10504.0` and `duckdb-go-bindings v0.10504.0` correspond to DuckDB 1.5.4. When bumping, also update `example/go.mod`/`go.sum` if a matching release exists.
- Commits in this repo carry DCO sign-off (`git commit -s`).

## Build details that are easy to break

The key invocation is `make bundle-library` run inside the DuckDB checkout with:

- `BUILD_EXTENSIONS="httpfs;parquet;icu;json;"`, `EXTENSION_STATIC_BUILD=1`, `DISABLE_EXTENSION_LOAD=1`, `BUILD_SHELL=0`, `GEN=ninja`
- gcc14 (`/usr/bin/gcc14-gcc`, `gcc14-g++`), `-Os`, `-ffunction-sections -fdata-sections`, `--gc-sections --strip-all`, then `objcopy --strip-debug`
- Per-arch `-march`: `armv8.2-a+crypto+fp16+dotprod+lse` on arm64 and `x86-64-v3` on amd64. The output will not run on older CPUs.
- CMake is installed from Kitware tarballs (a pinned `CMAKE_VER`) because AL2023's CMake is too old.

The extension set shows up in three places that must stay in sync: the matrix `build_extensions`, the README "What's Included?" table, and the release `body` text in the workflow. Don't rename the asset name suffix (`flavor: httpfs-parquet`), because `example/Makefile` greps releases for it.

## Local build testing: native arch only

When you test a build locally, build only for the host's own architecture (check with `uname -m`). Do not cross-compile. On an x86_64 host, don't try to build the arm64 bundle or the arm64 example, and on an aarch64 host, don't build amd64. That includes Docker `--platform` builds that run the other arch under QEMU emulation. CI builds each arch on a native runner (`ubuntu-24.04-arm` for arm64, `ubuntu-latest` for amd64), so leave the other arch to CI.

The example defaults to arm64 (`ARCH ?= arm64` in `example/Makefile`, `ARG GO_ARCH=arm64` in the Dockerfile, `Architectures: arm64` in `template.yaml`). On an x86_64 host, set `ARCH=amd64` explicitly for `make download-libs` and `sam build` (for example `ARCH=amd64 sam build`), or you will end up cross-building for arm64.

## Example (`example/`)

```bash
cd example
make download-libs            # fetch latest release asset into libs/parquet/ (ARCH=arm64 default; ARCH=amd64, FORCE=1 to refetch)
sam build                     # runs Makefile target build-DuckDBParquetFunction → Docker build → .aws-sam/build/
sam local invoke DuckDBParquetFunction
```

How the static link works (`example/Dockerfile`):

- The build image is AL2023 with gcc14, matching both CI and the Lambda runtime.
- It sets `CGO_ENABLED=1`, `CPPFLAGS=-DDUCKDB_STATIC_BUILD`, and `CGO_LDFLAGS="-L/src/libs/parquet -lduckdb_bundle -lstdc++ -lm -lcurl -lssl -lcrypto -lpthread -ldl"`.
- `go build -tags=duckdb_use_static_lib` makes `duckdb-go` link the local `.a` instead of its bundled prebuilt libs.
- The `exporter` stage copies `/src/data`, and `cmd/main.go` reads `data/imdb_processed.parquet` from the working directory. `data/`, `libs/` and `*.parquet` are gitignored, so `data/` must exist locally before building. `*.json` is also gitignored, so `env.json` is tracked only because it was force-added.
- The SAM resource name `DuckDBParquetFunction` must match the Makefile target `build-DuckDBParquetFunction`.
- Extensions are compiled in, so the code uses `LOAD 'parquet'` with no `INSTALL`. Loading from disk is disabled in the bundle.
- `GO_VERSION` is pinned both in `example/Makefile` and as the Dockerfile `ARG` default.

`example/README.md` is partly stale. It refers to `make download-libduckdb_bundle`, `DuckDBExampleFunction`, `samconfig.toml`, and an S3/httpfs query. The actual target is `download-libs`, the function is `DuckDBParquetFunction`, and the query is a local parquet read. `env.json` is still keyed by the old `DuckDBExampleFunction` name.

## Non-code artifacts

Issues, PR descriptions, specs, plans, reviews, and every other non-code artifact give readers the
context and judgment the diff cannot show, and are published in full on GitHub. The full rules:

@docs/non-code-rules.md

## PR rules

- Merge a PR only when I explicitly ask; squash-merge unless I say otherwise.
- When reviewing a PR, post everything (findings, spec and standards checks, assessment, observations, verification, summary) as one comment on the PR.
- After a PR is merged, clean up local branches and worktrees, fast-forward main, then update and close related issues.

## Git conventions

Never include AI attribution in commit messages, PR titles, or PR descriptions, in any form: no
`Co-Authored-By: Claude`, `Generated with ...` footers, sign-offs naming an AI agent or vendor
(Claude, Anthropic, GPT, OpenAI, …), or `Claude-Session:` trailers and session URLs — even when a
tool inserts them automatically. When squash-merging, write a clean commit message that describes
only the change itself.