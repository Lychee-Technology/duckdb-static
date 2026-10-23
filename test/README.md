# Tests

## Real HTTPS

To also run the fixture queries over real HTTPS, point `DUCKDB_TEST_HTTPFS_BASE_URL` at the fixtures on GitHub, pinned to a commit on `main`:

```bash
DUCKDB_TEST_HTTPFS_BASE_URL=https://raw.githubusercontent.com/Lychee-Technology/duckdb-static/$(git rev-parse origin/main)/test/fixtures/public \
  CC=gcc14-gcc test/smoke.sh libduckdb_bundle-amd64-linux-httpfs-parquet.tar.xz v2.0.0
```

After the local checks, `test/fixtures/fixtures.py verify` checks that the host serves the committed files with working byte ranges, and then the same queries run against it. With `GITHUB_TOKEN` set, requests to this repository's files on `raw.githubusercontent.com` send it as a bearer token, because GitHub limits anonymous downloads more tightly; it is never sent to another URL or printed. Without `DUCKDB_TEST_HTTPFS_BASE_URL`, the summary at the end says the remote run is not configured, which is not a pass.

The fixture checks set httpfs's retries (`http_retries`, `http_retry_wait_ms`, `http_timeout`) rather than re-running a failed query, so a wrong result can't be retried into a pass. On a `429` or `503`, httpfs adds up to five retries and honors `Retry-After` up to 20 s. Against GitHub, a run without retries makes 25 requests for the queries, plus 22 for the preflight.

In CI, each build job writes its tarball's sha256 to the log, the job summary and a `.sha256` file in its artifact. When the repository variable `DUCKDB_TEST_HTTPFS_BASE_URL` is set, the `httpfs-remote` job then runs `test/smoke.sh` with it on each architecture's native runner, in a fresh Amazon Linux 2023 container, after checking the downloaded tarball against that sha256. Only its smoke step gets the workflow's token as `GITHUB_TOKEN`. It refuses a URL on a GitHub host unless it has the form `https://raw.githubusercontent.com/<owner>/<repo>/<full commit SHA>/…`, because `http://`, another spelling of the host and `github.com`'s `/raw/` links all reach a branch's cached files too. It copies the smoke test's summary to its job summary, with a separate warning when the host answered `429`. The release doesn't wait for it. If the variable is unset when the run starts, the job shows as skipped; a change to the variable doesn't reach runs that have already started. To set it:

```bash
gh variable set DUCKDB_TEST_HTTPFS_BASE_URL \
  --body https://raw.githubusercontent.com/Lychee-Technology/duckdb-static/<commit>/test/fixtures/public
```

## Reading a failure

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
