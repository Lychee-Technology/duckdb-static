# Tests

What the tests cover, which of them gate a release, how to run them on your own machine, and how to read a failure. The behavior they check against is documented in the main README's [Linking and registering the extensions](../README.md#linking-and-registering-the-extensions) and [Upgrading from a DuckDB 1.x bundle](../README.md#upgrading-from-a-duckdb-1x-bundle). The Parquet fixtures' contract, and how they are generated and hosted, are in [`fixtures/README.md`](fixtures/README.md).

## Coverage

Apart from the packaging script's own checks, every test of the bundle's C API is in one harness. `smoke.sh` checks the tarball, then builds `smoke.c` against it three ways and runs each binary; the comments at the top of both files say how. Other workflows test something else: the fixtures themselves and the Go Lambda in `example/`. The benchmark in `example/bench/` measures rather than tests, but it checks results on the way.

| What is checked | Run by | Network | Blocks a release |
|---|---|---|---|
| The merged archive keeps every member of its input archives, defines the registration functions and each extension's `describe` function, and holds one machine type | `scripts/package-bundle.sh` in the build job | none | yes |
| The tarball holds exactly `libduckdb_bundle.a`, `duckdb.h` and `duckdb_static_extension.h`; every archive member is built for the host's architecture; both headers compile; each test binary needs only the shared libraries on an allowlist | `smoke.sh` in the build job | none | yes |
| Registration by a call, by `-Wl,-u,duckdb_register_static_extensions`, and not at all, which must leave the extensions missing; `INSTALL` and `LOAD` of an extension that isn't bundled fail | `smoke.c`, one binary per way | none | yes |
| Core SQL, nested JSON, ICU, local Parquet, and a database file reopened twice | `smoke.c` | none | yes |
| httpfs over plain HTTP, and over S3 with path-style URLs and `USE_SSL false` | `smoke.c` against `smoke.sh`'s server on 127.0.0.1 | loopback only | yes |
| The fixture queries through httpfs: exact results, only ranged `GET`s, a scan that fetches less than the whole file, a bearer token sent only within its scope, a missing file's 404 | `smoke.c` against the same server | loopback only | yes |
| The same fixture queries over real HTTPS, after `fixtures.py verify` checks the host: TLS through Amazon Linux 2023's libcurl, OpenSSL and CA bundle, and GitHub's byte ranges and rate limits | `smoke.sh` with `DUCKDB_TEST_HTTPFS_BASE_URL`, in the `httpfs-remote` job | internet | no |
| The fixtures regenerate byte for byte on arm64 and x86_64, and `fixtures.py`'s own tests | `fixtures.yml` | downloads the DuckDB CLI | no bundle involved |
| The Go Lambda builds against the bundle and returns the README's result under `sam local invoke` on `provided.al2023` | `example.yml` | pulls container images | no |
| Under `sam local invoke`, the bundle and duckdb-go's stock library each open DuckDB, load the five extensions with automatic installation off, and return the exact `sum(id)` of a local fixture, reporting the same DuckDB build; the time each step takes. Also, untimed, both read the fixture over HTTPS when `DUCKDB_TEST_HTTPFS_BASE_URL` is set | `benchmark.yml`, which runs `example/bench/run.sh` | pulls container images and the stock `httpfs` | no |

The local server is about 50 lines of Python. It serves files with byte ranges and logs each request's `Authorization` scheme, but it checks no credentials. So the S3 check shows that httpfs signs its requests, not that S3 would accept the signature, and no local check exercises TLS. Only the HTTPS row does, and only against the configured host, which in CI is `raw.githubusercontent.com`.

Nothing tests S3 over TLS or against AWS, virtual-hosted-style S3 URLs, a deployed Lambda function, CPUs older than the build's `-march` baseline, or a distribution other than Amazon Linux 2023. The HTTPS run uses an Amazon Linux 2023 container rather than Lambda's runtime image. [#24](https://github.com/Lychee-Technology/duckdb-static/pull/24) found that both images load the same CA file and have the same libcurl and OpenSSL versions, so a pass there is good evidence for Lambda, but not proof.

## Which bundle each run tests

| Trigger | What runs | On which bundle |
|---|---|---|
| A PR, or a push to `main`, that changes `example/` or `example.yml`; or a `workflow_dispatch` of `example.yml` on any ref | `example.yml` | The latest published release, whatever ref the run is on. If its DuckDB version differs from the ref's `duckdb_version`, the job skips the commands and still passes, with a `Skipped:` notice. A dispatch on a tag tests that tag's assets only while it is the latest release. |
| A PR, or a push to `main`, that changes `test/fixtures/` or `fixtures.yml`; or a `workflow_dispatch` of `fixtures.yml` on any ref | `fixtures.yml` | None |
| A PR that changes `duckdb_version`, `scripts/`, `test/` or `build-and-release.yml`, in a file that isn't Markdown | The build and smoke test on each architecture, then `example` and `httpfs-remote` | The tarballs this run built from the PR merged into its base. They are kept as artifacts for one day, and nothing is released. A new push to the PR cancels its earlier run. A PR from a fork runs only after a maintainer approves it. |
| Any other PR or push to `main`, including a push that changes the paths in the row above and a PR that changes only Markdown in them | Nothing | None. Dispatch the build on the branch. |
| `workflow_dispatch` of `build-and-release.yml` on a branch | The same jobs as a PR, plus `benchmark` on `main` or when the dispatch sets `benchmark` | The tarballs this run built. They are kept as artifacts for one day, and nothing is released. |
| `workflow_dispatch` of `benchmark.yml` with a build run's ID | `benchmark` | That run's tarballs, while its artifacts last |
| A pushed `v*` tag, or a dispatch on one | The same jobs, plus `benchmark` and `release` | The tarballs this run built. `release` publishes them only if both build jobs passed, after checking each against the sha256 its build recorded after the smoke test. |

The build job on each architecture, smoke test included, is the only gate: a release needs every check in the rows marked "yes" above to pass on both architectures. `example`, `httpfs-remote` and `benchmark` are separate signals. The main README's [Releasing](../README.md#releasing) has the policy, the guard against overwriting a release, how to read `httpfs-remote`, and what making it required involves.

None of these states is a pass:

- `httpfs-remote` shows as skipped: the repository variable `DUCKDB_TEST_HTTPFS_BASE_URL` was unset when the run started.
- `smoke.sh`'s summary says `remote: not configured`: nothing was read over HTTPS.
- `example` passes with a `Skipped:` notice: it ran none of the README's commands.
- A fixture run prints `stop the host failed, so the remaining fixture checks are skipped`: the checks after the first host or network failure didn't run.
- A PR or push to `main` outside the paths above has no bundle check at all.
- A PR's build run shows as cancelled: a newer push to the PR replaced it, and the newer run is the one to read.

## Running the tests

You need a host of the tarball's architecture: x86_64 for `amd64`, aarch64 for `arm64`. Don't run the other architecture under QEMU or with `--platform`; CI tests each one on a native runner. The CPU must also meet the build's baseline (see [Installation / Usage](../README.md#installation--usage)). The commands below use Docker and run in a fresh `amazonlinux:2023` container, as the `httpfs-remote` job does. With Podman on a host with SELinux enforcing, add `,Z` to each mount's `:ro`.

`smoke.sh` checks exact values for the DuckDB version in the checkout's `duckdb_version`, so test a tarball built from that version. To test one from a build run, download its artifact within a day of the run and check it against the sha256 recorded after its smoke test:

```bash
bundle_dir=$HOME/duckdb-bundle
gh run download <run id> -n libduckdb-amd64-httpfs-parquet -D "$bundle_dir"
(cd "$bundle_dir" && sha256sum -c libduckdb_bundle-amd64-linux-httpfs-parquet.tar.xz.sha256)
```

A tarball you downloaded from a release or built yourself (see [Building and testing](../README.md#building-and-testing)) works the same way; set `bundle_dir` to its directory. Then, from the repository's root:

```bash
docker run --rm -v "$PWD":/repo:ro -v "$bundle_dir":/bundle:ro -w /repo \
  public.ecr.aws/amazonlinux/amazonlinux:2023 bash -c '
    dnf install -y gcc14 gcc14-c++ libcurl-devel openssl-devel binutils findutils tar xz python3 &&
    . ./duckdb_version &&
    CC=gcc14-gcc test/smoke.sh /bundle/libduckdb_bundle-amd64-linux-httpfs-parquet.tar.xz "$DUCKDB_VER"'
```

On arm64, replace `amd64` with `arm64` in both commands. The run takes under a minute, most of it `dnf install`, and needs no network beyond that. It ends with a summary:

```
== summary
local:  passed
remote: not configured: DUCKDB_TEST_HTTPFS_BASE_URL is unset, so nothing was read over HTTPS
```

To test the fixtures themselves, see [Generating and checking](fixtures/README.md#generating-and-checking). To build and invoke the Lambda example against a tarball you have, set `BUNDLE_TARBALL` as described in [`example/README.md`](../example/README.md). To test the architecture your machine doesn't have, open a PR that changes one of the build's paths, or dispatch the build on your branch with `gh workflow run build-and-release.yml --ref <branch>`; either takes about half an hour.

### Real HTTPS

To also run the fixture queries over real HTTPS, point `DUCKDB_TEST_HTTPFS_BASE_URL` at the fixtures on GitHub, pinned to a commit on `main`. Add this option to the `docker run` command above:

```bash
  -e DUCKDB_TEST_HTTPFS_BASE_URL=https://raw.githubusercontent.com/Lychee-Technology/duckdb-static/$(git rev-parse origin/main)/test/fixtures/public
```

After the local checks, `test/fixtures/fixtures.py verify` checks that the host serves the committed files with working byte ranges, and then the same queries run against it. With `GITHUB_TOKEN` set, requests to this repository's files on `raw.githubusercontent.com` send it as a bearer token, because GitHub limits anonymous downloads more tightly; it is never sent to another URL or printed. Pass it into the container with `-e GITHUB_TOKEN`. Without `DUCKDB_TEST_HTTPFS_BASE_URL`, the summary at the end says the remote run is not configured, which is not a pass.

The fixture checks set httpfs's retries (`http_retries`, `http_retry_wait_ms`, `http_timeout`) rather than re-running a failed query, so a wrong result can't be retried into a pass. On a `429` or `503`, httpfs adds up to five retries and honors `Retry-After` up to 20 s. Against GitHub, a run without retries makes 25 requests for the queries, plus 22 for the preflight.

In CI, each build job writes its tarball's sha256 to the log, the job summary and a `.sha256` file in its artifact. When the repository variable `DUCKDB_TEST_HTTPFS_BASE_URL` is set, the `httpfs-remote` job then runs `test/smoke.sh` with it on each architecture's native runner, in a fresh Amazon Linux 2023 container, after checking the downloaded tarball against that sha256. Only its smoke step gets the workflow's token as `GITHUB_TOKEN`. It refuses a URL on a GitHub host unless it has the form `https://raw.githubusercontent.com/<owner>/<repo>/<full commit SHA>/…`, because `http://`, another spelling of the host and `github.com`'s `/raw/` links all reach a branch's cached files too. It copies the smoke test's summary to its job summary, with a separate warning when the host answered `429`. The release doesn't wait for it. If the variable is unset when the run starts, the job shows as skipped; a change to the variable doesn't reach runs that have already started. To set it:

```bash
gh variable set DUCKDB_TEST_HTTPFS_BASE_URL \
  --body https://raw.githubusercontent.com/Lychee-Technology/duckdb-static/<commit>/test/fixtures/public
```

## Reading a failure

The local and the remote run read the same committed files with the same queries, so where a failure happens says what is at fault:

- **The local run fails**, in the build job or on the `local:` line: the bundle, or the checkout's tests. Whatever the remote run did doesn't change that.
- **Only the remote run fails, exit 3:** the host or the network. Re-run it.
- **Only the remote run fails, exit 1:** the base URL or token can't be used, or a query gave a wrong result over HTTPS only. The latter points at something only real HTTPS exercises, such as TLS, and is a bundle bug. [The HTTPS check](../README.md#the-https-check) says what that means for a release.
- **`example` fails and the smoke test passes:** usually a `duckdb-go` version that doesn't match the bundle, or a registry that timed out while pulling an image. See [`example/AGENTS.md`](../example/AGENTS.md#ci).
- **`benchmark` fails and the smoke test passes:** the status at the top of its summary says why; [`example/bench/README.md`](../example/bench/README.md#how-a-run-is-judged) explains each. A variant being slower is never a failure.
- **`fixtures.yml` fails:** the fixtures' bytes or `fixtures.py`, not the bundle. See [`fixtures/README.md`](fixtures/README.md).

`test/smoke.sh` exits 0 when every check that ran passed, 3 when only the remote run failed and every failure was in the host or the network, and 1 otherwise: a failure that points at the bundle, or a `DUCKDB_TEST_HTTPFS_BASE_URL` or `GITHUB_TOKEN` that can't be used.

When the smoke test fails, a line starting with `FAIL` says what went wrong:

- `is not a readable .tar.xz` or `the tarball holds`: the tarball is damaged, or `scripts/package-bundle.sh` packaged the wrong files.
- `libduckdb_bundle.a is built for`: the tarball is for the other architecture, or the archive mixes objects from two.
- `doesn't build against the tarball's headers and archive`: a shipped header doesn't compile, or the archive lacks a symbol a consumer links, such as an extension's `describe` function. The compiler's error above it names the cause.
- `needs <library>, which isn't on smoke.sh's allowlist`: consumers would need a new shared library at run time. If that is intended, add the library to the allowlist in `test/smoke.sh` and to the run-time list in the main README's [Linking and registering the extensions](../README.md#linking-and-registering-the-extensions).
- `FAIL <query>`: DuckDB returned a wrong value or error. `<n> of <m> checks failed` follows. An error from the network or an HTTP server is labeled `[network: DNS]`, `[network: TLS]`, `[network: timeout]`, `[network: connection]`, `[host: HTTP <status>]` or `[host: byte ranges]`. After such an error in a fixture check, the remaining fixture checks are skipped. `[host: HTTP 429, rate-limited]` means GitHub's rate limit was hit after httpfs's retries; the bundle isn't at fault. The summary then ends with a `rate-limited:` line, which also appears when the preflight got a `429`.
- `did not report any passed checks`: a binary exited without running its checks. If it crashed, its output stops after the last check that ran, with no `FAIL` line.
- `the server logged no ... request`: an httpfs check passed without reading from the local server.
- `read the fixtures without the token` or `sent the fixtures' token outside its scope`: httpfs didn't apply an `http` secret's `BEARER_TOKEN` to exactly the URLs under its `SCOPE`, which the remote run relies on to keep `GITHUB_TOKEN` away from other hosts.
- `remote: failed the preflight`: the host doesn't serve the fixtures with working byte ranges, or the base URL points at the wrong place, such as a commit without the fixtures. `fixtures.py verify`'s `FAIL` lines above say which request failed.
- `remote: not run: DUCKDB_TEST_HTTPFS_BASE_URL isn't an http:// or https:// URL`: the variable is mistyped. This exits 1, not 3, so that automation doesn't take it for a host failure.

## Adding a check

Add it to `smoke.c` or `smoke.sh` rather than starting another harness, and don't repeat a check that is already there, such as registration, which every binary already tests one of three ways.

- **A query:** put it in the function for its area: `check_core`, `check_json`, `check_parquet` or `check_reopen`, or `main` for a single extension. `expect` compares one value, so fold a multi-row result into one with `list(... ORDER BY ...)::VARCHAR`, and check an error with `expect_error_type` where its `duckdb_error_type` is specific enough. Expected values are DuckDB's exact text. When a DuckDB bump changes how a value renders, update the expectation rather than loosening the check.
- **A check on the tarball or on what a binary links:** put it in `smoke.sh`, before the binaries are built, or in `check_needed`'s allowlist.
- **A query on remote Parquet:** it belongs in a new fixture version, because a committed version, its queries included, never changes. See [`fixtures/README.md`](fixtures/README.md) for the steps and for what `smoke.c` has to change to read the new version.
- **A new extension:** it changes several files that have to agree; [`AGENTS.md`](../AGENTS.md) lists them.

Update the coverage table above when a check adds to what the tests cover.
