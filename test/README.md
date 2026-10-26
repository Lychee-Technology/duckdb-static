# Tests

What the tests on the `duckdb-v1.5.x` branch cover, which of them gate a release, how to run them on your own machine, and how to read a failure. The behavior they check against is documented in the main README's [Linking](../README.md#linking), and where DuckDB 1.5.6 differs from `main`'s 2.0 in [`docs/backport-1.5.x.md`](../docs/backport-1.5.x.md#compatibility-matrix). The Parquet fixtures' contract, and how they are generated and hosted, are in [`fixtures/README.md`](fixtures/README.md).

## Coverage

Every test of the bundle's C API is in one harness. `smoke.sh` checks the tarball, then builds `smoke.c` against it once and runs the binary; the comments at the top of both files say how. `main` builds three binaries, one per way of registering the extensions, but DuckDB 1.5.6 has no registration step: opening a database loads every statically linked extension. Other workflows test something else: the fixtures themselves and the Go Lambda in `example/`. The benchmark in `example/bench/` measures rather than tests, but it checks results on the way.

| What is checked | Run by | Network | Blocks a release |
|---|---|---|---|
| The tarball holds exactly `libduckdb_bundle.a`; every archive member is built for the host's architecture; `smoke.c` builds against the archive and the `duckdb.h` of the DuckDB checkout the bundle was built from; the test binary needs only the shared libraries on an allowlist | `smoke.sh` in the build job | none | yes |
| The bundle reports `duckdb_version`'s DuckDB version; with no setup, `core_functions`, `httpfs`, `icu`, `json` and `parquet` are loaded as `STATICALLY_LINKED`; a query per extension gives its exact result; `LOAD` of a linked extension succeeds; `INSTALL` and `LOAD` of an extension that isn't bundled fail with a Permission Error | `smoke.c` | none | yes |
| Core SQL, nested JSON, ICU, local Parquet, and a database file reopened twice; afterwards, nothing beyond the five extensions is installed or loaded, and automatic installing and loading are still off | `smoke.c` | none | yes |
| httpfs over plain HTTP, and over S3 with path-style URLs | `smoke.c` against `smoke.sh`'s server on 127.0.0.1 | loopback only | yes |
| The fixture queries through httpfs: exact results, only ranged `GET`s, repeated lookups that fetch less than the whole file with the external file cache off, a bearer token sent only within its scope, a missing file's 404 | `smoke.c` against the same server | loopback only | yes |
| The same fixture queries over real HTTPS, after `fixtures.py verify` checks the host: TLS through Amazon Linux 2023's libcurl, OpenSSL and CA bundle, and GitHub's byte ranges and rate limits | `smoke.sh` with `DUCKDB_TEST_HTTPFS_BASE_URL`, in the `httpfs-remote` job | internet | no |
| The fixtures regenerate byte for byte on arm64 and x86_64, and `fixtures.py`'s own tests | `fixtures.yml` | downloads the DuckDB v2.0.0 CLI | no bundle involved |
| `example/go.mod`'s `duckdb-go` and `duckdb-go-bindings` encode `duckdb_version`'s DuckDB version; the Go Lambda builds against the bundle and returns the README's result under `sam local invoke` on `provided.al2023` | `example.yml` | pulls container images, and downloads the newest `v1.5.6_<n>` release outside a build run | no |
| Under `sam local invoke`, the bundle and `duckdb-go-bindings`' stock static library each open DuckDB, load the five extensions with automatic installation off, the bundle's built in and the stock `httpfs` from the package, and return the exact `sum(id)` of `v2/tiny.parquet` read over HTTP from a local fixture server. Both must link DuckDB statically and report `duckdb_version`'s DuckDB and the same source commit; the run records the time each step takes and the requests each read made. Also, untimed, both read a v1 fixture over HTTPS when `DUCKDB_TEST_HTTPFS_BASE_URL` is set | `benchmark.yml`, which runs `example/bench/run.sh` | pulls container images and the stock `httpfs`; the timed reads stay on a local container network | no |
| The benchmark's own tests: the summarizer's status decisions and statistics, and that `run.sh` stops before the fixture server on each `incomparable` case it can detect up front and removes the server's container and network when setting them up fails | `test_summarize.py`, and `test_run.py` with a stand-in for `docker`, in `benchmark.yml` | none | no bundle involved |

The local server is `fixtures/serve.py`. It serves files with byte ranges and logs each request's `Authorization` scheme, but it checks no credentials. So the S3 check shows that httpfs signs its requests, not that S3 would accept the signature, and no local check exercises TLS. Only the HTTPS row does, and only against the configured host, which in CI is `raw.githubusercontent.com`.

Nothing tests S3 over TLS or against AWS, virtual-hosted-style S3 URLs, a deployed Lambda function, CPUs older than the build's `-march` baseline, or a distribution other than Amazon Linux 2023. The HTTPS run uses an Amazon Linux 2023 container rather than Lambda's runtime image. On `main`, [#24](https://github.com/Lychee-Technology/duckdb-static/pull/24) found that both images load the same CA file and have the same libcurl and OpenSSL versions, so a pass there is good evidence for Lambda, but not proof. The benchmark and the example run under SAM's emulator on a GitHub runner or your machine, with warm page caches; their numbers aren't Lambda cold starts.

Also not tested, because of how 1.5.6 differs from `main`:

- A C++ program that opens a database with `DBConfigOptions::load_extensions` set to `false`, the only way to get one without the five extensions. Nothing reachable from C, Go or SQL does that.
- The archive's member count. `make bundle-library` extracts each input archive into a directory of its own, so members that share a name survive, and a fixed count would break on any DuckDB patch release. Removing an extension's main object breaks the smoke test's link, but a member that nothing links would go unnoticed. [`docs/backport-1.5.x.md`](../docs/backport-1.5.x.md#compatibility-matrix) has the reasoning.

## Which bundle each run tests

| Trigger | What runs | On which bundle |
|---|---|---|
| A PR into, or a push to, `duckdb-v1.5.x` that changes `example/` or `example.yml`; or a `workflow_dispatch` of `example.yml` on a ref | `example.yml` | The newest release tagged `<DUCKDB_VER>_<n>`, with the ref's `duckdb_version`, found by `example/release-url.sh`; never the release marked Latest. If no such release exists, the README jobs show as skipped, with an `Example skipped` notice. If `go.mod` doesn't match `duckdb_version`, the `Versions` job fails and the README jobs don't run. A dispatch on a tag tests that tag's assets only while it is the newest release of its DuckDB version. |
| A PR into any branch, or a push to `duckdb-v1.5.x`, that changes `test/fixtures/` or `fixtures.yml`; or a `workflow_dispatch` of `fixtures.yml` on a ref | `fixtures.yml` | None |
| A PR into `duckdb-v1.5.x` that changes `duckdb_version`, `test/` or `build-and-release.yml`, in a file that isn't Markdown | The build and smoke test on each architecture, then `example` and `httpfs-remote` | The tarballs this run built from the PR merged into its base. They are kept as artifacts for one day, and nothing is released. A new push to the PR cancels its earlier run. |
| A push to `duckdb-v1.5.x` with the same paths, such as a PR's merge | The same jobs, plus `benchmark` | The tarballs this run built from the pushed commit |
| Any other PR into or push to `duckdb-v1.5.x`, including a push that changes the paths in the rows above only in Markdown | Nothing that builds a bundle | None. Dispatch the build on the branch. A change to `example/bench/` runs `example.yml`, which doesn't run the benchmark or its tests, and a change to `benchmark.yml` alone runs nothing; dispatch the build with `-f benchmark=true` to test either. |
| `workflow_dispatch` of `build-and-release.yml` on a branch | The same jobs as a PR, plus `benchmark` on `duckdb-v1.5.x` or when the dispatch sets `benchmark` | The tarballs this run built. They are kept as artifacts for one day, and nothing is released. |
| `workflow_dispatch` of `benchmark.yml` on `duckdb-v1.5.x` with a build run's ID | `benchmark` | That run's tarballs, while its artifacts last. Without `--ref duckdb-v1.5.x`, the dispatch runs `main`'s `benchmark.yml`, which builds against `main`'s DuckDB 2.0 `go.mod`. |
| A pushed `v*` tag, or a dispatch on one | The same jobs, plus `benchmark` and `release` | The tarballs this run built. The build fails unless the tag is `<DUCKDB_VER>_<n>` and comes after `v1.5.6_0`. `release` publishes the tarballs only if both build jobs passed, after checking each against the sha256 its build recorded after the smoke test. |

The build job on each architecture, smoke test included, is the only gate: a release needs every check in the rows marked "yes" above to pass on both architectures. `example`, `httpfs-remote` and `benchmark` are separate signals. The main README's [Releasing](../README.md#releasing) has the policy, the guard against overwriting a release, and [The HTTPS check](../README.md#the-https-check) says how to read `httpfs-remote` and what making it required involves.

None of these states is a pass:

- `httpfs-remote` shows as skipped: the repository variable `DUCKDB_TEST_HTTPFS_BASE_URL` was unset when the run started.
- `smoke.sh`'s summary says `remote: not configured`: nothing was read over HTTPS.
- The example's README jobs show as skipped, with an `Example skipped` notice: no release of `duckdb_version`'s DuckDB exists, so no README command ran.
- A fixture run prints `stop the host failed, so the remaining fixture checks are skipped`: the checks after the first host or network failure didn't run.
- The benchmark's summary has a status other than `valid`. Its job fails then too.
- A PR or push outside the paths above has no bundle check at all.
- A PR's build run shows as cancelled: a newer push to the PR replaced it, and the newer run is the one to read.

## Running the tests

You need a host of the tarball's architecture: x86_64 for `amd64`, aarch64 for `arm64`. Don't run the other architecture under QEMU or with `--platform`; CI tests each one on a native runner. The CPU must also meet the build's baseline (see [Installation / Usage](../README.md#installation--usage)). The commands below use Docker and run in a fresh `amazonlinux:2023` container, as the `httpfs-remote` job does. With Podman on a host with SELinux enforcing, add `,Z` to each mount's `:ro`.

`smoke.sh` takes the tarball, the DuckDB version it must report, and a directory holding the matching `duckdb.h`. It checks exact values for the DuckDB version in the checkout's `duckdb_version`, so test a tarball built from that version. To test one from a build run, download its artifact and its header within a day of the run, and check the tarball against the sha256 recorded after its smoke test:

```bash
bundle_dir=$HOME/duckdb-bundle
gh run download <run id> -n libduckdb-amd64-httpfs-parquet -n duckdb-header-amd64 -D "$bundle_dir"
(cd "$bundle_dir/libduckdb-amd64-httpfs-parquet" && sha256sum -c libduckdb_bundle-amd64-linux-httpfs-parquet.tar.xz.sha256)
```

Then, from the repository's root:

```bash
docker run --rm -v "$PWD":/repo:ro -v "$bundle_dir":/bundle:ro -w /repo \
  public.ecr.aws/amazonlinux/amazonlinux:2023 bash -c '
    dnf install -y gcc14 gcc14-c++ libcurl-devel openssl-devel binutils findutils tar xz python3 &&
    . ./duckdb_version &&
    CC=gcc14-gcc test/smoke.sh /bundle/libduckdb-amd64-httpfs-parquet/libduckdb_bundle-amd64-linux-httpfs-parquet.tar.xz "$DUCKDB_VER" /bundle/duckdb-header-amd64'
```

On arm64, replace `amd64` with `arm64` in both commands. The run takes under a minute, most of it `dnf install`, and needs no network beyond that. It ends with a summary:

```
== summary
local:  passed
remote: not configured: DUCKDB_TEST_HTTPFS_BASE_URL is unset, so nothing was read over HTTPS
```

A released tarball has no header next to it. Put DuckDB v1.5.6's `src/include/duckdb.h` in the directory you pass instead, and check a download from `v1.5.6_1` on against the release's `SHA256SUMS`; `v1.5.6_0`'s sha256s are in [`docs/backport-1.5.x.md`](../docs/backport-1.5.x.md#what-a-15x-release-contains). For example:

```bash
bundle_dir=$HOME/duckdb-release
gh release download <tag> -p 'libduckdb_bundle-amd64-*' -p SHA256SUMS -D "$bundle_dir"
(cd "$bundle_dir" && sha256sum -c --ignore-missing SHA256SUMS)   # v1.5.6_1 and later
curl -fsSL -o "$bundle_dir/duckdb.h" https://raw.githubusercontent.com/duckdb/duckdb/v1.5.6/src/include/duckdb.h
```

and run the `docker` command above with `/bundle/libduckdb_bundle-amd64-linux-httpfs-parquet.tar.xz "$DUCKDB_VER" /bundle` as `smoke.sh`'s arguments. A tarball you built yourself (see [Building](../README.md#building-and-testing)) works the same way, with the `duckdb.h` of the checkout you built it from.

To test the fixtures themselves, see [Generating and checking](fixtures/README.md#generating-and-checking). To build and invoke the Lambda example against a tarball you have, set `BUNDLE_TARBALL` as described in [`example/README.md`](../example/README.md). To run the benchmark, see [`example/bench/README.md`](../example/bench/README.md#running-it); its self-tests need only Python 3: `python3 example/bench/test_summarize.py` and `python3 example/bench/test_run.py`. To test the architecture your machine doesn't have, open a PR that changes one of the build's paths, or dispatch the build on your branch with `gh workflow run build-and-release.yml --ref <branch>`; either takes about half an hour.

### Real HTTPS

To also run the fixture queries over real HTTPS, point `DUCKDB_TEST_HTTPFS_BASE_URL` at the fixtures on GitHub, pinned to a commit on `main`. Add this option to the `docker run` command above:

```bash
  -e DUCKDB_TEST_HTTPFS_BASE_URL=https://raw.githubusercontent.com/Lychee-Technology/duckdb-static/$(git rev-parse origin/main)/test/fixtures/public
```

After the local checks, `test/fixtures/fixtures.py verify` checks that the host serves the committed files with working byte ranges, and then the same queries run against it. With `GITHUB_TOKEN` set, the queries' requests to this repository's files on `raw.githubusercontent.com` send it as a bearer token, while the preflight always reads without it, because GitHub limits anonymous downloads more tightly; it is never sent to another URL or printed. Pass it into the container with `-e GITHUB_TOKEN`. Without `DUCKDB_TEST_HTTPFS_BASE_URL`, the summary at the end says the remote run is not configured, which is not a pass.

The fixture checks set httpfs's retries (`http_retries`, `http_retry_wait_ms`, `http_timeout`) rather than re-running a failed query, so a wrong result can't be retried into a pass. DuckDB 1.5.6's httpfs retries any error status, `429` and `503` included, 3 more times in about 1.5 s, and ignores `Retry-After`; `main`'s 2.0 adds retries on a `429` or `503` and honors `Retry-After`. The preflight makes 22 requests.

In CI, each build job writes its tarball's sha256 to the log, the job summary and a `.sha256` file in its artifact. When the repository variable `DUCKDB_TEST_HTTPFS_BASE_URL` is set, the `httpfs-remote` job then runs `test/smoke.sh` with it on each architecture's native runner, in a fresh Amazon Linux 2023 container, after checking the downloaded tarball against that sha256 and with the header artifact of the same run. Only its smoke step gets the workflow's token as `GITHUB_TOKEN`. It refuses a URL that isn't `https://`, because the job is the only check of TLS, and a URL on a GitHub host unless it has the form `https://raw.githubusercontent.com/<owner>/<repo>/<full commit SHA>/…`, because another spelling of the host and `github.com`'s `/raw/` links all reach a branch's cached files too. It copies the smoke test's summary to its job summary, with a separate warning when the host answered `429`. The release doesn't wait for it. If the variable is unset when the run starts, the job shows as skipped; a change to the variable doesn't reach runs that have already started. To set it:

```bash
gh variable set DUCKDB_TEST_HTTPFS_BASE_URL \
  --body https://raw.githubusercontent.com/Lychee-Technology/duckdb-static/<commit>/test/fixtures/public
```

The variable is the repository's, so it applies to `main`'s runs too.

## Reading a failure

The local and the remote run read the same committed files with the same queries, so where a failure happens says what is at fault:

- **The local run fails**, in the build job or on the `local:` line: the bundle, or the checkout's tests. Whatever the remote run did doesn't change that.
- **Only the remote run fails, exit 3:** the host or the network. Re-run it.
- **Only the remote run fails, exit 1:** the base URL or token can't be used, or a query gave a wrong result over HTTPS only. The latter points at something only real HTTPS exercises, such as TLS, and is a bundle bug. [The HTTPS check](../README.md#the-https-check) says what that means for a release.
- **`example`'s `Versions` job fails:** `example/go.mod`'s `duckdb-go` versions don't encode `duckdb_version`'s DuckDB version. Bump them together.
- **An `example` README job fails and the smoke test passes:** usually a registry that timed out while pulling an image, or, outside a build run, a release whose bundle doesn't match the bindings. See [`example/AGENTS.md`](../example/AGENTS.md#ci).
- **`benchmark` fails and the smoke test passes:** the status at the top of its summary says why; [`example/bench/README.md`](../example/bench/README.md#how-a-run-is-judged) explains each. A variant being slower is never a failure. A `not run` whose warm-up got `no response; see sam.log` can be SAM failing to pull its runtime image: in [#55](https://github.com/Lychee-Technology/duckdb-static/pull/55)'s benchmark dispatch, `sam.log` showed a timeout reaching `public.ecr.aws`, and a re-run of the job passed.
- **`fixtures.yml` fails:** the fixtures' bytes or `fixtures.py`, not the bundle. See [`fixtures/README.md`](fixtures/README.md).

`test/smoke.sh` exits 0 when every check that ran passed, 3 when only the remote run failed and every failure was in the host or the network, and 1 otherwise: a failure that points at the bundle, or a `DUCKDB_TEST_HTTPFS_BASE_URL` or `GITHUB_TOKEN` that can't be used.

When the smoke test fails, a line starting with `FAIL` says what went wrong:

- `has no duckdb.h`: the third argument isn't the directory holding the header.
- `is not a readable .tar.xz` or `the tarball holds`: the tarball is damaged, or the build packaged the wrong files.
- `libduckdb_bundle.a is built for`: the tarball is for the other architecture, or the archive mixes objects from two.
- `no expected archive machine type for a <machine> host`: the host is neither x86_64 nor aarch64, the only architectures the bundle is built for.
- `doesn't build against`: the header doesn't match the archive, or the archive lacks a symbol the program links, such as a dropped extension member. The compiler's error above it names the cause.
- `needs no shared libraries, expected at least libc`: `readelf -d` listed no `NEEDED` entry for the test binary. Linked dynamically, as `smoke.sh`'s link line asks, the binary always needs libc, so either `readelf` failed or the compiler linked it statically. Either way, the allowlist check had nothing to check, and consumers' run-time dependencies are unknown.
- `needs <library>, which isn't on smoke.sh's allowlist`: consumers would need a new shared library at run time. If that is intended, add the library to the allowlist in `test/smoke.sh` and to the main README's [Linking](../README.md#linking).
- `FAIL <query>`: DuckDB returned a wrong value or error. `<n> of <m> checks failed` follows. An error from the network or an HTTP server is labeled `[network: DNS]`, `[network: TLS]`, `[network: timeout]`, `[network: connection]`, `[host: HTTP <status>]`, `[host: HTTP]` (an HTTP error without a status) or `[host: byte ranges]`. After such an error in a fixture check, the remaining fixture checks are skipped. `[host: HTTP 429, rate-limited]` means GitHub's rate limit was hit after httpfs's retries; the bundle isn't at fault. The summary then ends with a `rate-limited:` line, which also appears when the preflight got a `429`.
- `did not report any passed checks`: the binary exited 0 without ending on `<n> checks passed`. A binary that ran no check exits 1 and prints `no checks ran` instead. If it crashed, its output stops after the last check that ran, with no `FAIL` line.
- `the server logged no ... request`: an httpfs check passed without reading from the local server, or the fixtures' bearer token wasn't sent where it should have been.
- `read the fixtures without the token scoped to them` or `sent the fixtures' token outside its scope`: httpfs didn't apply the `TYPE http` secret's `BEARER_TOKEN` to exactly the URLs under its `SCOPE`, which the remote run relies on to keep `GITHUB_TOKEN` away from other hosts. The request lines above it are the ones at fault.
- `GITHUB_TOKEN has a quote, a space or a control character`: the remote run didn't start, because the token can't go into an SQL string and an HTTP header.

When the remote run doesn't pass, the summary's `remote:` line says why. Two of its values need explaining:

- `remote: failed the preflight`: the host doesn't serve the fixtures with working byte ranges, or the base URL points at the wrong place, such as a commit without the fixtures. `fixtures.py verify`'s `FAIL` lines below `== preflight of <base URL>` say which request failed; [`fixtures/README.md`](fixtures/README.md#verifying-the-host) explains them. This exits 3.
- `remote: not run: DUCKDB_TEST_HTTPFS_BASE_URL isn't an http:// or https:// URL`: the variable is mistyped. This exits 1, not 3, so that automation doesn't take it for a host failure.

## Adding a check

Add it to `smoke.c` or `smoke.sh` rather than starting another harness, and don't repeat a check that is already there.

- **A query:** put it in the function for its area: `check_core`, `check_json`, `check_parquet` or `check_reopen`, or `main` for a single extension. `expect` compares one value, so fold a multi-row result into one with `list(... ORDER BY ...)::VARCHAR`, and check an error with `expect_error_type` where its `duckdb_error_type` is specific enough. Expected values are DuckDB's exact text. When a DuckDB bump changes how a value renders, update the expectation rather than loosening the check.
- **A check on the tarball or on what the binary links:** put it in `smoke.sh`, before the binary is built, or in `check_needed`'s allowlist.
- **A query on remote Parquet:** it belongs in a new fixture version, because a committed version, its queries included, never changes. See [`fixtures/README.md`](fixtures/README.md) for the steps and for what `smoke.c` has to change to read the new version.
- **A new extension:** it changes several files that have to agree; [`AGENTS.md`](../AGENTS.md) lists them.
- **A check ported from `main`:** run it against a 1.5.6 bundle before relying on `main`'s expected value, and record any difference in [`docs/backport-1.5.x.md`](../docs/backport-1.5.x.md). `main`'s registration checks don't apply here.

Update the coverage table above when a check adds to what the tests cover.
