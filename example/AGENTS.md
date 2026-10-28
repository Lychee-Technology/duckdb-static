# AGENTS.md

Guidance for `example/` on the `duckdb-v1.5.x` branch, which builds DuckDB 1.5.6. The repo-wide rules in `../AGENTS.md` still apply, including native-arch-only local builds and DCO sign-off.

This is a Go Lambda (AWS SAM, `provided.al2023`) that links a released `libduckdb_bundle.a` and queries a parquet file shipped with the function. `README.md` is the user-facing version of this file. When the Makefile, Dockerfile, template, or query changes, update it too. CI runs the README's commands, so a README that no longer matches the example fails CI (see [CI](#ci)).

## Build and run

On an arm64 host (the default):

```bash
make download-libs            # fetch the newest v1.5.6_<n> release's asset into libs/parquet/ (FORCE=1 to refetch, BUNDLE_TARBALL=<file> to use a local bundle)
sam build                     # runs Makefile target build-DuckDBParquetFunction → Docker build → .aws-sam/build/
sam local invoke DuckDBParquetFunction
```

On an x86_64 host:

```bash
ARCH=amd64 make download-libs
ARCH=amd64 sam build --parameter-overrides Architecture=x86_64
sam local invoke DuckDBParquetFunction --parameter-overrides Architecture=x86_64
```

The architecture is set in two places that must agree. `ARCH` in `Makefile` (default `arm64`) picks the release asset and the Docker/Go target. The Dockerfile's `ARG GO_ARCH=arm64` is only a fallback, because the Makefile always passes `GO_ARCH`. The `Architecture` parameter in `template.yaml` (default `arm64`) sets the function's Lambda architecture, which selects the runtime image for `sam local invoke` and the architecture `sam deploy` creates.

Overriding only `ARCH` puts an amd64 binary in an arm64 function. Overriding neither builds and runs arm64 under emulation. The values differ because the release assets say `amd64` and Lambda says `x86_64`. `sam build` passes its environment to `make`, which is how `ARCH` reaches the Makefile. The built template still references the parameter, so `sam local invoke` and `sam deploy` need the override as well.

## How the build works

- The build image is AL2023 with gcc14, matching both CI and the Lambda runtime.
- It sets `CGO_ENABLED=1`, `CPPFLAGS=-DDUCKDB_STATIC_BUILD`, and `CGO_LDFLAGS="-L/src/libs/parquet -lduckdb_bundle -lstdc++ -lm -lcurl -lssl -lcrypto -lpthread -ldl"`.
- `go build -tags=duckdb_use_static_lib` makes `duckdb-go` link the local `.a` instead of its bundled prebuilt libs.
- The `data` stage generates `data/sample.parquet` with the `duckdb/duckdb` CLI image, and the `exporter` stage puts it next to `bootstrap`. `cmd/main.go` reads it from the working directory (`/var/task` on Lambda), so a clean checkout needs no local data. The image tag doesn't have to match the bundle's DuckDB version. The stage's SQL produces an average of exactly 10.5 tokens per row, which `README.md` gives as the expected output.
- The SAM resource name `DuckDBParquetFunction` must match the Makefile target `build-DuckDBParquetFunction`.
- Extensions are compiled in, so the code uses `LOAD 'parquet'` with no `INSTALL`. Loading from disk is disabled in the bundle.
- `GO_VERSION` is pinned both in `Makefile` and as the Dockerfile `ARG` default.
- The `duckdb-go` versions in `go.mod` encode the DuckDB version: `duckdb-go/v2 v2.10506.0` and `duckdb-go-bindings v0.10506.0` correspond to DuckDB 1.5.6. Unless `BUNDLE_TARBALL` is set, `make download-libs` fetches the newest release of the DuckDB version in `../duckdb_version`, the `<DUCKDB_VER>_<n>` tag with the highest `<n>`, which `release-url.sh` finds. It never takes the release marked Latest, which is `main`'s DuckDB 2.0 build: bindings before `v0.20000.0` don't register a 2.0 bundle's extensions, so `LOAD 'parquet'` fails. When `duckdb_version` changes, update `go.mod`/`go.sum` to the matching `duckdb-go` version. `example.yml` fails until they agree.

## CI

`.github/workflows/example.yml` runs the README's command blocks on native runners: arm64 on `ubuntu-24.04-arm` and x86_64 on `ubuntu-latest`. It fails unless the output has a line equal to the README's expected output. It reads the commands and the expected line from `README.md`, from the code block right after each `<!-- .github/workflows/example.yml ... -->` comment, so editing a block changes what CI runs. Keep the comments and their text as they are. A missing comment, or an expected-output block that isn't exactly one line, fails the job.

It tests one of two bundles:

- On PRs into and pushes to `duckdb-v1.5.x` that touch `example/` or the workflow, and on manual dispatch, `make download-libs` fetches the newest release of `duckdb_version`'s DuckDB version, as it does for users. The `Versions` job looks for that release first. If there is none, as between changing `duckdb_version` and releasing that version, the README jobs are skipped: they show as skipped, not passed, and an `Example skipped` notice on the run says why. The build workflow's run for the same change tests the example against the bundles it builds.
- `build-and-release.yml` calls it after the build, for tag pushes, dispatches, and PRs and pushes that change the build's inputs. Each README job checks the run's tarball against the sha256 the build recorded after its smoke test, then sets `BUNDLE_TARBALL` to it, so the README's `make download-libs` extracts the bundle just built and tested. The release job doesn't wait for this check, so a DuckDB release can ship before a matching `duckdb-go` exists.

In both, the `Versions` job fails unless the `duckdb-go/v2` and `duckdb-go-bindings` versions in `go.mod` encode the DuckDB version in `duckdb_version`, and the README jobs then don't run. A DuckDB bump therefore fails this check until `go.mod` is bumped too, rather than passing with bindings for another version. Each README job's summary names the bundle it ran: the release asset's URL, or the artifact's path and sha256.

The job pulls three images with no retries. From ECR Public it pulls `amazonlinux:2023` for the build and `lambda/provided:al2023`, which `sam local invoke` builds its runtime image on. From Docker Hub it pulls `duckdb/duckdb` anonymously, in the Dockerfile's `data` stage. A registry timeout fails the job: `Get "https://public.ecr.aws/v2/": context deadline exceeded` happened once during `sam local invoke`. If a job failed while pulling, re-run it before suspecting the change. In a build-workflow run, re-run it within a day: the job downloads that run's bundles, and the artifacts expire after one day, so a later re-run fails at the download. After that, dispatch the build again for a branch. For a tag that is still the newest release of its DuckDB version, dispatch this workflow on the tag (`gh workflow run example.yml --ref refs/tags/<tag>`), which tests the published assets, the same files the artifacts held. Don't re-run the whole tag run, because its release job would overwrite the release's assets with a rebuild. No Docker Hub rate-limit errors have shown up so far. If they do, the job needs Docker Hub credentials.

## Benchmark

`bench/` times DuckDB's readiness and a first Parquet query over HTTP in a Lambda process, with the bundle and with `duckdb-go`'s stock library. `bench/README.md` has what it measures and how to run it. It is a second main package in this module so that both variants always build with the `duckdb-go` in this `go.mod`, but it has its own Dockerfile, template and scripts and changes nothing the example builds. Because it lives under `example/`, a PR that changes it also runs `example.yml`. `benchmark-tests.yml` runs its self-tests on such a PR unless it changes only Markdown; the benchmark itself never runs on one.

The stock library is `github.com/duckdb/duckdb-go-bindings/lib/linux-<arch>` at the version `go.mod` requires: static archives, with no `provenance.json`. When `go.mod` and `duckdb_version` name different DuckDB versions, the benchmark reports `incomparable` until `go.mod` is bumped. Its stock `httpfs` comes from `extensions.duckdb.org` for the version in `duckdb_version`, so a DuckDB release that DuckDB hasn't published `httpfs` for is `incomparable` too.

The query reads `test/fixtures/public/v2/tiny.parquet` from `test/fixtures/serve.py`, which `run.sh` runs in an `amazonlinux:2023` container on a Docker network of the run's own; `sam local invoke --docker-network` puts the Lambda containers on it, and they reach the server as `http://fixtures:8080`. The expected `sum(id)`, `720600`, is in `v2/manifest.json` and in `expectedSum` in `bench/main.go`, and `bench/test_summarize.py` checks that the two agree. A run with Podman needs the `docker` command and SAM's API socket to reach the same user's Podman, so the network `run.sh` creates is the one SAM attaches to.

The extension list appears in `extensions` in `bench/main.go` and `EXTENSIONS` in `bench/summarize.py`, and the libraries the bundle needs in `bench/Dockerfile`'s `CGO_LDFLAGS`; keep them in step with the build and `test/smoke.sh`.

## Podman

The build and `sam local invoke` work with rootless Podman instead of Docker. This was tested on an x86_64 Fedora host with SELinux enforcing. Three things differ from Docker:

- The Makefile runs `docker build`, so it needs a `docker` command that runs Podman, such as a symlink to `podman` on `PATH` or Fedora's `podman-docker` package. A shell alias doesn't reach `make`.
- SAM talks to the Docker API, so point it at Podman's API socket: run `systemctl --user start podman.socket`, then `export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/podman/podman.sock`.
- With SELinux enforcing, `sam local invoke` fails with `fork/exec /var/task/bootstrap: permission denied`, because the build directory is mounted into the runtime container without an SELinux label that allows it. Run a separate Podman API service with labeling turned off, and point SAM at that one instead:

```bash
printf '[containers]\nlabel = false\n' > "$XDG_RUNTIME_DIR/sam-nolabel.conf"
CONTAINERS_CONF_OVERRIDE="$XDG_RUNTIME_DIR/sam-nolabel.conf" \
  podman system service --time=0 "unix://$XDG_RUNTIME_DIR/sam-podman.sock" &
export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/sam-podman.sock"
```

This turns off SELinux separation only for containers started through that socket, so other Podman containers keep it. Stop the service and remove the socket and config file when you're done. Unix socket paths must be shorter than 108 bytes, so keep the socket under `$XDG_RUNTIME_DIR` rather than a deep scratch directory.
