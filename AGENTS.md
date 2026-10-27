# AGENTS.md

## What this repo is

This repo holds no DuckDB source. It is a CI recipe plus an example. The GitHub Actions workflow (`.github/workflows/build-and-release.yml`) clones upstream DuckDB at a pinned tag, builds `libduckdb_bundle.a` (DuckDB plus statically linked extensions) inside an Amazon Linux 2023 container, and publishes it to GitHub Releases as `libduckdb_bundle-{arm64,amd64}-linux-httpfs-parquet.tar.xz`. The goal is fast cold starts on AWS Lambda (`provided.al2023`) with no extension downloads at runtime.

This is the `duckdb-v1.5.x` maintenance branch, which builds DuckDB 1.5.6; `main` builds DuckDB 2.0. `docs/backport-1.5.x.md` says where 1.5.6 differs from 2.0, what a 1.5.x release contains, how each of `main`'s commits was handled, and how to work on this branch. Branch from `duckdb-v1.5.x`, open PRs against it, and never merge `main` into it.

There are no linters. The repo has these parts:

- `duckdb_version`: a shell-sourceable file (`DUCKDB_VER`). CI `source`s it, so keep it in `KEY=value` form. The build fails unless it names a DuckDB 1.5.x version.
- `.github/workflows/build-and-release.yml`: the whole build. Each build job smoke-tests its tarball, then records its sha256 in its artifact and uploads DuckDB's `duckdb.h` as the `duckdb-header-<arch>` artifact. When the repository variable `DUCKDB_TEST_HTTPFS_BASE_URL` is set, the `httpfs-remote` job runs `test/smoke.sh` with it on both arches' bundles, in a fresh AL2023 container; unset, the job shows as skipped.
- `test/smoke.sh` and `test/smoke.c`: called by the build after packaging. They check that the tarball holds only `libduckdb_bundle.a`, built for the host's machine type, link a C program against it with the header from the build's DuckDB checkout, and check a query result for each extension, over HTTP and S3 for httpfs against a local server. They also check core SQL, nested JSON, local Parquet and a reopened database file, that `INSTALL` and `LOAD` of any other extension fail with a Permission Error, that the checks left no other extension installed or loaded, and that the test binary needs only the shared libraries on an allowlist. The fixture queries from `test/fixtures/` run through httpfs against the local server and, when `DUCKDB_TEST_HTTPFS_BASE_URL` is set, against that public HTTPS host; exit status 3 means only the host or the network failed. A check compares one value, so fold a multi-row result into one with `list(... ORDER BY ...)::VARCHAR`, and check an error by its `duckdb_error_type` (`expect_error_type`) rather than its message where the type is specific enough.
- `test/README.md`: what the tests cover and which run gates a release, how to run them in a fresh AL2023 container on a CI artifact without building, and what each `FAIL` line means. When a check adds to what the tests cover, or a workflow changes what runs on which bundle, update its tables.
- `test/fixtures/`: Parquet fixtures for testing httpfs against a public HTTPS site, with `fixtures.py` to generate, check and verify them, and `serve.py`, the range server `test/smoke.sh` and the benchmark serve them with. It is the same as on `main` apart from its README. A committed fixture version never changes; new data goes into a new version. `test/fixtures/README.md` has the contract and the hosting steps.
- `.github/workflows/fixtures.yml`: regenerates the fixtures on native arm64 and x86_64 runners, fails unless the bytes match the committed ones, and runs `test/fixtures/test_fixtures.py`.
- `.github/workflows/example.yml`: runs `example/README.md`'s build-and-invoke commands on native arm64 and x86_64 runners and fails unless the output matches the README. `example/AGENTS.md` explains when it runs and which bundle it tests.
- `example/`: a Go Lambda (AWS SAM) that consumes a released bundle. Its guidance is in `example/AGENTS.md`.
- `example/bench/` and `.github/workflows/benchmark.yml`: times DuckDB's readiness and a first query on `test/fixtures/public/v2/tiny.parquet` over HTTP, with the bundle and with `duckdb-go-bindings`' stock static library, under `sam local invoke`. `run.sh` serves the fixture with `test/fixtures/serve.py` in a container on a Docker network of the run's own, which the Lambda containers join with `--docker-network`. It shares `example/go.mod` but has its own Dockerfile. `build-and-release.yml` runs it in runs on `duckdb-v1.5.x` and on tags, never on PRs, and the release doesn't wait for it. `example/bench/README.md` says what it measures and how a run is judged.

## Release flow

- A pushed `v*` tag builds both arches and creates a GitHub Release, not marked Latest: Latest stays `main`'s newest DuckDB 2.0 release, which `main`'s example downloads as `releases/latest`. Alongside the release, CI runs the example, `httpfs-remote` and the benchmark against the bundles it just built; the release waits for none of them. Before uploading, the release checks each `*.tar.xz` against the `.sha256` its build recorded after the smoke test, and publishes the tarballs plus one `SHA256SUMS`, never a `<tarball>.sha256` (see the asset-name note below). Only the release job can write to the repository. Every `actions/checkout` sets `persist-credentials: false`: no job runs an authenticated git command, and most then run code from the PR, which could read a token left in `.git/config`.
- Tags for this line are `v<DUCKDB_VER>_<n>`, such as `v1.5.6_1`, pushed on this branch's commits; `<n>` is a rebuild counter for the same DuckDB version. The build fails before compiling if the tag names another version, or is `v1.5.6_0` or earlier, which earlier workflows already released. A `v*` tag on a commit of `main` runs `main`'s workflow and releases a 2.0 bundle, which nothing on this branch can stop.
- A PR into `duckdb-v1.5.x`, or a push to it, that changes `duckdb_version`, `test/` or `build-and-release.yml`, other than in Markdown files, runs the same jobs on its own bundles, without releasing. A new push to the PR cancels its earlier run. PRs never run the benchmark. A `workflow_dispatch` run on a branch builds, uploads artifacts (1-day retention), and runs the example and `httpfs-remote` against them, so dispatch on a branch to test any other change. It also runs the benchmark on `duckdb-v1.5.x`, or on another branch with `-f benchmark=true`.
- A dispatch on a tag also releases, because the release job checks only `github.ref_type == 'tag'`. If the tag already has a release, even a draft, the release job fails unless the dispatch sets `overwrite-release`, which replaces the assets. A tag push isn't checked. The README's "Releasing" section has the gating policy, including the decision, taken on `main` in #28 and kept here, to leave `httpfs-remote` non-blocking.
- GitHub closes an issue from `Closes #N` only when the PR merges into the default branch, so close a backport issue by hand after its PR merges here.
- `duckdb_version` stays on DuckDB 1.5.x, and `example/go.mod` on the `duckdb-go` version that encodes it (`v2.10506.0` for 1.5.6). Never use `v2.2xxxx` here: those versions are for DuckDB 2.0. `example/AGENTS.md` explains the mapping. A patch release, such as 1.5.7, changes both files, and `example.yml` fails until they agree.
- Commits in this repo carry DCO sign-off (`git commit -s`).

## Build details that are easy to break

The build runs DuckDB 1.5.x's own `make bundle-library` in the DuckDB checkout: `make release`, then every static archive it produced (engine, third-party libraries, the generated extension loader and each extension) is extracted into a directory of its own and all their objects go into `build/release/libduckdb_bundle.a`. There is no packaging script. The invocation uses:

- `BUILD_EXTENSIONS='httpfs;parquet;icu;json;'`, plus `EXTENSION_STATIC_BUILD=1`, `DISABLE_EXTENSION_LOAD=1`, `BUILD_SHELL=0`, `GEN=ninja` and `DUCKDB_PLATFORM`. DuckDB's `extension/extension_config.cmake` always adds `core_functions`, so the bundle has five extensions.
- gcc14 (`/usr/bin/gcc14-gcc`, `gcc14-g++`), `-Os`, `-ffunction-sections -fdata-sections`, `--gc-sections --strip-all`, then `objcopy --strip-debug` on the bundle
- Per-arch `-march`: `armv8.2-a+crypto+fp16+dotprod+lse` on arm64 and `x86-64-v3` on amd64. The output will not run on older CPUs.
- CMake is installed from Kitware tarballs (a pinned `CMAKE_VER`), not from AL2023's packages.

None of `main`'s DuckDB 2.0 settings apply: the 1.5.6 bundle reports `v1.5.6` without `DUCKDB_VERSION`, and 1.5.6 has no `DUCKDB_NEW_EXTENSION_BUILD` or `STATICALLY_LINK_EXTENSIONS`. Nothing has to register the extensions: opening a database loads every statically linked one. The tarball holds only `libduckdb_bundle.a`, as every release since `v1.5.3_1` has; `test/smoke.sh` fails on any other file, and a header in it would change what consumers extract. The smoke test compiles against the build's `duckdb/src/include/duckdb.h`.

The extension set shows up in several places that must stay in sync: the matrix `build_extensions`, the README "What's Included?" table, the release `body` text in the workflow, whose README link needs the README's `Linking` heading, the expected list in `test/smoke.c`, and `extensions` in `example/bench/main.go` with `EXTENSIONS` in `example/bench/summarize.py`. Likewise, the shared libraries a consumer needs appear in the README's link line and run-time list, in the NEEDED allowlist in `test/smoke.sh`, and in `example/bench/Dockerfile`'s `CGO_LDFLAGS`. Don't rename the asset name suffix (`flavor: httpfs-parquet`), and don't publish an asset whose name contains a tarball's, because `example/release-url.sh`, which `make download-libs` calls, matches release assets with an unanchored regex and `.github/workflows/example.yml`, `.github/workflows/benchmark.yml` and the `httpfs-remote` job download the build's artifacts by name. Keep the header artifact's name outside the release job's `libduckdb-*` download pattern, or the release would publish it.

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
