# AGENTS.md

## What this repo is

This repo holds no DuckDB source. It is a CI recipe plus an example. The GitHub Actions workflow (`.github/workflows/build-and-release.yml`) clones upstream DuckDB at a pinned tag, builds `libduckdb_bundle.a` (DuckDB plus statically linked extensions) inside an Amazon Linux 2023 container, and publishes it to GitHub Releases as `libduckdb_bundle-{arm64,amd64}-linux-httpfs-parquet.tar.xz`. The goal is fast cold starts on AWS Lambda (`provided.al2023`) with no extension downloads at runtime.

There are no linters. The repo has these parts:

- `duckdb_version`: a shell-sourceable file (`DUCKDB_VER`). CI `source`s it, so keep it in `KEY=value` form.
- `.github/workflows/build-and-release.yml`: the whole build.
- `scripts/package-bundle.sh`: called by the build. It merges DuckDB's engine and extension archives into `libduckdb_bundle.a` and writes the release tarball.
- `test/smoke.sh` and `test/smoke.c`: called by the build after packaging. They link a C program against the tarball and check a query result for each extension, over HTTP and S3 for httpfs against a local server. They also check core SQL, nested JSON, local Parquet and a reopened database file, and that the extensions are missing without registration. A check compares one value, so fold a multi-row result into one with `list(... ORDER BY ...)::VARCHAR`, and check an error by its `duckdb_error_type` (`expect_error_type`) rather than its message where the type is specific enough.
- `test/fixtures/`: Parquet fixtures for testing httpfs against a public HTTPS site, with `fixtures.py` to generate, check and verify them. A committed fixture version never changes; new data goes into a new version. `test/fixtures/README.md` has the contract and the hosting steps.
- `.github/workflows/fixtures.yml`: regenerates the fixtures on native arm64 and x86_64 runners, fails unless the bytes match the committed ones, and runs `test/fixtures/test_fixtures.py`.
- `.github/workflows/example.yml`: runs `example/README.md`'s build-and-invoke commands on native arm64 and x86_64 runners and fails unless the output matches the README. `example/AGENTS.md` explains when it runs and which bundle it tests.
- `example/`: a Go Lambda (AWS SAM) that consumes a released bundle. Its guidance is in `example/AGENTS.md`.

## Release flow

- If you push a tag matching `v*`, CI builds both arches and creates a GitHub Release. Alongside the release, it runs the example against the bundles it just built; the release doesn't wait for that check. A `workflow_dispatch` run on a branch only builds, uploads artifacts (1-day retention), and runs the example against them, so dispatch on a branch to test a build. A dispatch on a tag also releases, because the release job checks only `github.ref_type == 'tag'`: it creates that tag's release, or updates an existing one and overwrites its assets.
- Tags follow `v<duckdb-version>_<n>`, for example `v1.5.5_0` and `v1.5.4_1`. `<n>` is a rebuild counter for the same DuckDB version.
- To bump DuckDB, edit `DUCKDB_VER` in `duckdb_version`. Also update `example/go.mod`/`go.sum` if a matching `duckdb-go` release exists. `example/AGENTS.md` explains how `duckdb-go` versions map to DuckDB versions. Until the new version is released, the example check on PRs and `main` skips its commands but still passes, so its green check tested nothing; dispatch the build on the branch to test the example against the new bundle.
- Commits in this repo carry DCO sign-off (`git commit -s`).

## Build details that are easy to break

DuckDB 2.0 removed `make bundle-library`. The build runs `make release` inside the DuckDB checkout, and `scripts/package-bundle.sh` assembles the bundle from its output. The `make release` invocation uses:

- `BUILD_EXTENSIONS` and `STATICALLY_LINK_EXTENSIONS` both set to `core_functions;parquet;json;icu;httpfs`, plus `EXTENSION_STATIC_BUILD=1`, `DISABLE_EXTENSION_LOAD=1`, `BUILD_SHELL=0`, `GEN=ninja`
- `DUCKDB_VERSION=$DUCKDB_VER`. DuckDB 2.0 no longer takes its version from the git tag, so without this the bundle reports `v2.0.0-dev1`. The smoke test fails on a wrong version.
- `DUCKDB_NEW_EXTENSION_BUILD=0`. 2.0's new extension build path refuses httpfs unless vcpkg is set up. The legacy path fetches httpfs at the commit DuckDB pins and links it against AL2023's libcurl and OpenSSL, as 1.5.x builds did. DuckDB calls this path a temporary escape hatch, so a later DuckDB release may need vcpkg here.
- gcc14 (`/usr/bin/gcc14-gcc`, `gcc14-g++`), `-Os`, `-ffunction-sections -fdata-sections`, `--gc-sections --strip-all`, then `objcopy --strip-debug` on the bundle
- Per-arch `-march`: `armv8.2-a+crypto+fp16+dotprod+lse` on arm64 and `x86-64-v3` on amd64. The output will not run on older CPUs.
- CMake is installed from Kitware tarballs (a pinned `CMAKE_VER`) because AL2023's CMake is too old.

Since 2.0, `libduckdb_static.a` doesn't register any extension. `scripts/package-bundle.sh` adds one object compiled from DuckDB's generated `static_extension_loader.c` and `static_extension_autoregister.cpp`. The two are in one translation unit, so a consumer that pulls in `duckdb_register_static_extensions`, by calling it or with `-Wl,-u,duckdb_register_static_extensions`, also gets the before-main registration. The script merges the archives with an `ar` MRI script, which keeps members that share a name across archives (`platform.cpp.o` is in both `libduckdb_static.a` and `libparquet_extension.a`). Re-archiving extracted objects only keeps both if they reach `ar` in one call. It fails if the member count, the registration symbols, or the machine type are wrong.

The extension set shows up in several places that must stay in sync: the matrix `build_extensions`, which also sets the registration order, the README "What's Included?" table, its registration section and build example, the release `body` text in the workflow, and the expected list in `test/smoke.c`. Don't rename the asset name suffix (`flavor: httpfs-parquet`), because `example/Makefile` greps releases for it and `.github/workflows/example.yml` downloads the build's artifacts by name.

## Local build testing: native arch only

When you test a build locally, build only for the host's own architecture (check with `uname -m`). Do not cross-compile. On an x86_64 host, don't try to build the arm64 bundle or the arm64 example, and on an aarch64 host, don't build amd64. That includes Docker `--platform` builds that run the other arch under QEMU emulation. CI builds each arch on a native runner (`ubuntu-24.04-arm` for arm64, `ubuntu-latest` for amd64), so leave the other arch to CI.

The example defaults to arm64. `example/AGENTS.md` gives the commands for an x86_64 host and explains the two architecture settings that must agree.

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