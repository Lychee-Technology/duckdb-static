# AGENTS.md

## What this repo is

This repo holds no DuckDB source. It is a CI recipe plus an example. The GitHub Actions workflow (`.github/workflows/build-and-release.yml`) clones upstream DuckDB at a pinned tag, builds `libduckdb_bundle.a` (DuckDB plus statically linked extensions) inside an Amazon Linux 2023 container, and publishes it to GitHub Releases as `libduckdb_bundle-{arm64,amd64}-linux-httpfs-parquet.tar.xz`. The goal is fast cold starts on AWS Lambda (`provided.al2023`) with no extension downloads at runtime.

There are no linters. The repo has four parts:

- `duckdb_version`: a shell-sourceable file (`DUCKDB_VER`, `DUCKDB_EXTENSION_CI_TOOLS_BRANCH`). CI `source`s it, so keep it in `KEY=value` form.
- `.github/workflows/build-and-release.yml`: the whole build.
- `.github/workflows/example.yml`: the only test. It runs `example/README.md`'s build-and-invoke commands on native arm64 and x86_64 runners and fails unless the output matches the README. `example/AGENTS.md` explains when it runs and which bundle it tests.
- `example/`: a Go Lambda (AWS SAM) that consumes a released bundle. Its guidance is in `example/AGENTS.md`.

## Release flow

- If you push a tag matching `v*`, CI builds both arches and creates a GitHub Release. Alongside the release, it runs the example against the bundles it just built; the release doesn't wait for that check. A `workflow_dispatch` run on a branch only builds, uploads artifacts (1-day retention), and runs the example against them, so dispatch on a branch to test a build. A dispatch on a tag also releases, because the release job checks only `github.ref_type == 'tag'`: it creates that tag's release, or updates an existing one and overwrites its assets.
- Tags follow `v<duckdb-version>_<n>`, for example `v1.5.5_0` and `v1.5.4_1`. `<n>` is a rebuild counter for the same DuckDB version.
- To bump DuckDB, edit both values in `duckdb_version`. Also update `example/go.mod`/`go.sum` if a matching `duckdb-go` release exists. `example/AGENTS.md` explains how `duckdb-go` versions map to DuckDB versions. Until the new version is released, the example check on PRs and `main` skips its commands but still passes, so its green check tested nothing; dispatch the build on the branch to test the example against the new bundle.
- Commits in this repo carry DCO sign-off (`git commit -s`).

## Build details that are easy to break

The key invocation is `make bundle-library` run inside the DuckDB checkout with:

- `BUILD_EXTENSIONS="httpfs;parquet;icu;json;"`, `EXTENSION_STATIC_BUILD=1`, `DISABLE_EXTENSION_LOAD=1`, `BUILD_SHELL=0`, `GEN=ninja`
- gcc14 (`/usr/bin/gcc14-gcc`, `gcc14-g++`), `-Os`, `-ffunction-sections -fdata-sections`, `--gc-sections --strip-all`, then `objcopy --strip-debug`
- Per-arch `-march`: `armv8.2-a+crypto+fp16+dotprod+lse` on arm64 and `x86-64-v3` on amd64. The output will not run on older CPUs.
- CMake is installed from Kitware tarballs (a pinned `CMAKE_VER`) because AL2023's CMake is too old.

The extension set shows up in three places that must stay in sync: the matrix `build_extensions`, the README "What's Included?" table, and the release `body` text in the workflow. Don't rename the asset name suffix (`flavor: httpfs-parquet`), because `example/Makefile` greps releases for it and `.github/workflows/example.yml` downloads the build's artifacts by name.

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