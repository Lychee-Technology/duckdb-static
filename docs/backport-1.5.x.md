# DuckDB 1.5.x maintenance branch

`duckdb-v1.5.x` builds and releases DuckDB 1.5.6 bundles. It was created on 2026-10-24 at tag `v1.5.6_0` (`212bc187f53d8934c6657bbec438e6c247131a86`) and never takes main's DuckDB 2.0 upgrade (#11). Epic #37 brings main's tests, CI, release checks and benchmark to this branch. This file is that work's reference: what DuckDB 1.5.6 does where it differs from 2.0, what a 1.5.x release contains, and how each of main's commits is handled. Each backport PR updates the parts it changes, and #45 records the final mapping.

## How the facts were checked

- **Published assets:** the two tarballs of release `v1.5.6_0`, downloaded on 2026-10-24.
- **A build from this branch:** run [40078585538](https://github.com/Lychee-Technology/duckdb-static/actions/runs/40078585538) dispatched the tag's own `build-and-release.yml`, unchanged, on this branch. Both arches built (arm64 in 15 minutes, amd64 in 19), and the release job was skipped because the ref isn't a tag. The rebuilt tarballs aren't byte-identical to the published ones (amd64 `fac02478…`, arm64 `5019f2df…`) but behave the same in every probe.
- **A probe of each bundle:** a C program built with DuckDB v1.5.6's `src/include/duckdb.h` and gcc14 on Amazon Linux 2023, linked against `libduckdb_bundle.a` with the README's link line, ran 127 queries against an in-memory database with no setup. They cover extension state, one query per extension, loading and installing, error types, main's httpfs settings, and every query in both fixture manifests read from disk and over HTTP from `test/fixtures/serve.py`. It ran on the published bundle and the run's bundle for each arch, on native runners in run [40079930033](https://github.com/Lychee-Technology/duckdb-static/actions/runs/40079930033). All four gave the same results. The source and logs are in the PR that added this file.
- **DuckDB's source** at tag `v1.5.6` (`069cc9f9b5be802405797faecc284961b07c70ef`, which the bundle reports as its `source_id`) and the Go modules at the versions `example/go.mod` pins.

## What a 1.5.x release contains

Releases `v1.5.3_1` through `v1.5.6_0` have two assets and nothing else:

| Asset | Contents | sha256 at `v1.5.6_0` |
|---|---|---|
| `libduckdb_bundle-amd64-linux-httpfs-parquet.tar.xz` | `./libduckdb_bundle.a` only (155 MB) | `e33925ea10dd2a5ba99e087828c941ebdb57863d4af76f5099364fe84661ee75` |
| `libduckdb_bundle-arm64-linux-httpfs-parquet.tar.xz` | `./libduckdb_bundle.a` only (140 MB) | `20340f24d04681286ad2edc4c06c130526be12b49499385c3d112ff103c8bec8` |

Earlier releases, `v1.5.1_1` through `v1.5.2_1`, have six: these two plus `-lance` and `-vortex` variants of each. Commit `6a8f3aa` removed those flavors, and this branch builds only `httpfs-parquet`.

There is no header and no checksum file. Consumers take `duckdb.h` from DuckDB v1.5.6 or from `duckdb-go-bindings` v0.10506.0, whose `include/duckdb.h` is byte-identical to it. A program linked with `-lduckdb_bundle -lstdc++ -lm -lcurl -lssl -lcrypto -lpthread -ldl` needs only `libstdc++`, `libm`, `libcurl`, `libssl`, `libcrypto`, `libgcc_s`, `libc` and the dynamic loader at run time, the same allowlist main's smoke test enforces. The loader (`ld-linux-x86-64.so.2` on amd64, `ld-linux-aarch64.so.1` on arm64) is the binary's ELF interpreter on both arches, but only amd64 also lists it among the `DT_NEEDED` entries the smoke test reads.

Main's 2.0 releases add `duckdb.h`, `duckdb_static_extension.h` and a `SHA256SUMS` asset. Whether 1.5.x releases add anything is #39's decision. This file only records the current contract.

## Compatibility matrix

| Area | DuckDB 1.5.6 (this branch) | DuckDB 2.0 (main) | Consequence |
|---|---|---|---|
| Build | `make bundle-library`: after `make release`, it extracts every static archive (engine, third-party libraries, extension loader, extensions) into a directory of its own and archives all their objects with `ar cr` | `make release`, then `scripts/package-bundle.sh` merges the static archives with an `ar` MRI script | 1.5.x needs no `package-bundle.sh` (#39) |
| Extensions | `BUILD_EXTENSIONS='httpfs;parquet;icu;json;'`, plus `core_functions`, which DuckDB's `extension/extension_config.cmake` always loads | `BUILD_EXTENSIONS` and `STATICALLY_LINK_EXTENSIONS` both `core_functions;parquet;json;icu;httpfs` | Same five extensions |
| Version string | The bundle reports `v1.5.6` with no version variable set | Needs `DUCKDB_VERSION`, or it reports `v2.0.0-dev1` | The smoke test's version check carries over |
| Extension build path | The only path 1.5.6 has | `DUCKDB_NEW_EXTENSION_BUILD=0` forces the legacy path | Nothing to port |
| Registration | None. `DuckDB::DuckDB` calls `ExtensionHelper::LoadAllExtensions` whenever a database opens, unless `DBConfigOptions::load_extensions` is `false` (`src/main/database.cpp:342-344`). The option defaults to `true` and is only reachable from C++: it is neither a SQL setting nor a `duckdb_set_config` option | The program must call `duckdb_register_static_extensions()` or link with `-Wl,-u,duckdb_register_static_extensions` | See [Registration checks](#registration-checks) |
| Registration symbols | `duckdb_register_static_extension(s)` are not defined; `generated_extension_loader.cpp.o` holds the loader | Both defined by the loader object `package-bundle.sh` adds | Main's symbol checks don't apply (#39) |
| `duckdb_extensions()` | With no setup, `core_functions`, `httpfs`, `icu`, `json` and `parquet` are loaded with `install_mode` `STATICALLY_LINKED`; nothing else is installed. The columns are `extension_name, loaded, installed, install_path, description, aliases, extension_version, install_mode, installed_from` | Same five, `STATICALLY_LINKED`, after registration | Main's extension-list check carries over (#39) |
| `LOAD` of a linked extension | `LOAD parquet` and `LOAD httpfs` succeed and do nothing | Same | `example/cmd/main.go`'s `LOAD` keeps working |
| `INSTALL` and `LOAD` of anything else | Permission Error (`DUCKDB_ERROR_PERMISSION`, 34): "Installing external extensions is disabled through a compile time flag", or "Loading …" for `LOAD`. `FORCE INSTALL parquet` and `INSTALL httpfs` fail the same way | Fails with "this build does not link the loadable_extensions library" | Main's `NO_LOADABLE` message must change; check the error type rather than the text (#39) |
| Autoloading | `autoinstall_known_extensions`, `autoload_known_extensions` and `allow_unsigned_extensions` are all `false` by default | Same settings, set to `false` by the fixture checks | Carries over |
| Archive | 626 members on both arches, with 163 names used more than once, because `ar` stores only the basename of objects from different archives; every member has the host's machine type | Merged by an MRI script that keeps members sharing a name; `package-bundle.sh` fails on a wrong member count | Main's member-count check needs a 1.5.x value or doesn't apply (#39) |
| Tarball | `./libduckdb_bundle.a` only | Archive plus two headers | Main's three-file check must not be copied (#39) |
| Header for tests | Not in the tarball; DuckDB v1.5.6's `src/include/duckdb.h` | Shipped in the tarball | Tests need a header from the build, not the tarball (#39, #41) |
| Run-time libraries | The allowlist above | Same allowlist | Carries over |
| Fixtures `v1` and `v2` | All 38 manifest queries (34 v1, 4 v2) give the committed results, from disk and over HTTP. The files were generated by the DuckDB v2.0.0 CLI | Same | Reuse the committed bytes; no 1.5.x fixture version is needed (#40) |
| httpfs | Reads over HTTP and over S3 with path-style URLs against the local server. A missing file is an HTTP Error (38) with "404". A `TYPE http` secret with `BEARER_TOKEN` and `SCOPE` sends the token on every request under its scope and on none outside it. Every GET of a fixture asked for a byte range and got only that range | Same | Main's httpfs checks carry over (#41) |
| httpfs settings | 12 of the 13 settings main's fixture checks set exist. `duckdb_logs_parsed('HTTP')`, `truncate_duckdb_logs()` and `enable_external_file_cache` work. **`external_file_cache_remote_max_block_size` doesn't exist** (Catalog Error) | All 13 exist | Main uses that setting so scans of `multi_row_group.parquet` fetch only the ranges they need. #41 must reach that behavior another way, or record why the check is dropped |
| Error types | Parser 14, Catalog 13, Binder 24, Conversion 2, Out of Range 1, Constraint 18 (primary key and NOT NULL), IO 28, HTTP 38, Permission 34 | Main's `expect_error_type` checks use the same names | Carries over (#40) |
| Go bindings | `duckdb-go/v2 v2.10506.0`, `duckdb-go-bindings v0.10506.0`. Neither module calls a registration function, and none is needed | `v2.20000.0` / `v0.20000.0`, whose `init` calls `duckdb_register_static_extensions()` | Keep the tag's `go.mod`; never use `v2.2xxxx` on this branch (#44) |
| Stock library for the benchmark | Module `github.com/duckdb/duckdb-go-bindings/lib/linux-{amd64,arm64}` v0.10506.0: static archives (`libduckdb_static.a` plus one per extension), linked into the Go binary. It has `core_functions`, `icu`, `json`, `parquet`, `tpch`, `tpcds` and `autocomplete`, no httpfs, and no `provenance.json` | Module `go.duckdb.org/libduckdb/linux-*` with a `provenance.json` | #43 changes the module path and records the missing provenance as such. DuckDB's `httpfs` build for v1.5.6 is available at `extensions.duckdb.org/v1.5.6/linux_{amd64,arm64}/httpfs.duckdb_extension.gz` for the stock variant |
| Example's bundle | `example/Makefile` downloads `releases/latest`, which has been `v2.0.0_0` since 2026-10-24, so the tag's README commands now fetch a 2.0 bundle for 1.5.6 bindings | Same target, and `example.yml` skips when the latest release doesn't match `duckdb_version` | #44 selects the release by version; #42 keeps 1.5.x releases from becoming Latest |
| CI | `build-and-release.yml` only: triggered by `v*` tags and dispatch, no smoke test, no PR trigger, `contents: write` for every job, releases `./release/*` with no overwrite guard and no `make_latest` | Smoke test, PR builds, sha256-verified release, overwrite guard, example, `httpfs-remote` and benchmark jobs | PRs into this branch run no checks until #42 adds a PR trigger |

`duckdb_version` also sets `DUCKDB_EXTENSION_CI_TOOLS_BRANCH=v1.5.6`. The workflow exports it, but nothing in DuckDB v1.5.6's `Makefile`, `CMakeLists.txt` or CMake modules reads it. Main dropped it with #11.

### Registration checks

Main's smoke test builds three binaries: one calls `duckdb_register_static_extensions()`, one links with `-Wl,-u,duckdb_register_static_extensions`, and one does neither and must find the extensions missing. None of these applies to 1.5.6. It has no registration function, and every database opened through the C API, or from C++ with the default `DBConfig`, has all five extensions loaded. Only a C++ caller that sets `load_extensions` to `false` gets a database without them. A consumer of the C API, which includes the Go bindings, can't opt out, so there is no unregistered build to test.

What does carry over is checked by one ordinary build: the five extensions report `STATICALLY_LINKED` with no setup, and `INSTALL` and `LOAD` of an extension outside the bundle fail with a Permission Error. That second check is the one that shows a query can't download an extension.

## Commit mapping

Main's commits after `v1.5.6_0`, oldest first. "Take" means cherry-pick unchanged, "adapt" means port with the changes listed, and "skip" means don't port.

| Commit | PR | What it adds on main | Decision | Owner | Changes for 1.5.x |
|---|---|---|---|---|---|
| `dcf690d` | #6 | `AGENTS.md`, `CLAUDE.md`, `docs/non-code-rules.md`; example `Architecture` parameter | Adapt | #44, #45 | Take `CLAUDE.md`, `docs/non-code-rules.md` and the example parameter. Rewrite `AGENTS.md` for 1.5.x: main's later version describes 2.0's build |
| `3dfc731` | #9 | Example builds from a clean checkout; `example/AGENTS.md` | Take | #44 | Its `example/AGENTS.md` already maps v2.10506.0 to DuckDB 1.5.6. #44 then replaces its "`make download-libs` always fetches the latest release" with the version-based selection |
| `8eb2388` | #10 | `example.yml`, which runs the README's commands on both arches | Adapt | #44 | Select the release by `duckdb_version`, not `releases/latest` |
| `63b92ec` | #12 | `klauspost/compress` 1.18.6 → 1.18.7 in `example/go.mod` | Take | #44 | The tag's `go.mod` has 1.18.6, so it applies |
| `3061172` | #11 | DuckDB 2.0: build, `package-bundle.sh`, the first `test/smoke.{sh,c}`, `go.mod` on v2.20000.0 | Skip | — | Its `test/smoke.sh` and `test/smoke.c` are the base that #22–#25 change. #39 ports their version-independent parts. Nothing else comes over |
| `4c60a86` | #21 | Fixtures v1, `fixtures.py`, `fixtures.yml`, `test_fixtures.py` | Take | #40 | `fixtures.py` keeps generating with the v2.0.0 CLI: generation doesn't depend on the bundle, and 1.5.6 reads the result |
| `01ad856` | #22 | Core SQL, JSON, Parquet and reopen checks | Adapt | #40 | Check each expected rendering against 1.5.6 |
| `d3f534a` | #23 | Tarball contents, machine type, headers and NEEDED checks | Adapt | #39 | Tarball holds only the archive; the header comes from the build; machine and NEEDED checks unchanged |
| `da11acf` | #24 | Fixture queries through httpfs locally and against `DUCKDB_TEST_HTTPFS_BASE_URL` | Adapt | #41 | Replace `external_file_cache_remote_max_block_size` |
| `d0f1a21` | #25 | `httpfs-remote` job | Adapt | #41, #42 | The job compiles the smoke test, so it needs the header artifact |
| `8db3657` | #27 | sha256 recorded after the smoke test and checked at release; overwrite guard | Adapt | #42 | Add `make_latest: false`; rewrite the release body without the 2.0 registration paragraph |
| `436b3ac` | #29 | `test/README.md` coverage and triage | Adapt | #45 | Describe the 1.5.x checks |
| `e612c28` | #30 | PR builds when build or test inputs change | Adapt | #42 | Must fire for PRs into this branch |
| `c382af9` | #34 | SAM benchmark of the bundle against duckdb-go's stock library | Adapt | #43 | Stock module path, missing `provenance.json`, the job's `if` allowing this branch |
| `a2a311b` | #35 | Records that `httpfs-remote` stays non-blocking | Take | #41, #45 | Same policy here |

Most adaptations edit `test/smoke.c` and `test/smoke.sh`, so the order is #39 → #40 → #41, then #42 with #44, then #43, then #45.

Most commits also edit `AGENTS.md` and `README.md`, which describe 2.0 on main. Even a "take" commit conflicts there, so port those hunks by hand and keep the 1.5.x facts above.

## Working on this branch

- Branch from `duckdb-v1.5.x` and open PRs against it. Never merge main into it.
- A PR into this branch runs no checks until #42 lands. Until then, test a change by dispatching `build-and-release.yml` on the PR's branch, which builds both arches and uploads the bundles without releasing.
- GitHub closes an issue from `Closes #N` only when the PR merges into the default branch. Close backport issues by hand after their PR merges here.
- Release tags for this line are `v1.5.6_<n>` with `n` ≥ 1, pushed on this branch's commits. Until #42 lands, a tag runs the tag's workflow: no smoke test, no overwrite guard, and no `make_latest: false`, so don't tag before then. After a release, point the README's installation link at the new tag.
