# AGENTS.md

Guidance for `example/`. The repo-wide rules in `../AGENTS.md` still apply, including native-arch-only local builds and DCO sign-off.

This is a Go Lambda (AWS SAM, `provided.al2023`) that links a released `libduckdb_bundle.a` and queries a parquet file shipped with the function. `README.md` is the user-facing version of this file. When the Makefile, Dockerfile, template, or query changes, update it too. CI runs the README's commands, so a README that no longer matches the example fails CI (see [CI](#ci)).

## Build and run

On an arm64 host (the default):

```bash
make download-libs            # fetch latest release asset into libs/parquet/ (FORCE=1 to refetch, BUNDLE_TARBALL=<file> to use a local bundle)
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
- The `duckdb-go` versions in `go.mod` encode the DuckDB version: `duckdb-go/v2 v2.10506.0` and `duckdb-go-bindings v0.10506.0` correspond to DuckDB 1.5.6. `make download-libs` always fetches the latest release. When that release has a newer DuckDB, update `go.mod`/`go.sum` to the matching `duckdb-go` version.

## CI

`.github/workflows/example.yml` runs the README's command blocks on native runners: arm64 on `ubuntu-24.04-arm` and x86_64 on `ubuntu-latest`. It fails unless the output has a line equal to the README's expected output. It reads the commands and the expected line from `README.md`, from the code block right after each `<!-- .github/workflows/example.yml ... -->` comment, so editing a block changes what CI runs. Keep the comments and their text as they are. A missing comment, or an expected-output block that isn't exactly one line, fails the job.

It tests one of two bundles:

- On PRs and pushes to `main` that touch `example/`, and on manual dispatch, `make download-libs` fetches the latest release, as it does for users. If that release's DuckDB version (its tag without `_<n>`) differs from `duckdb_version`, the job skips with a notice instead. That happens between pushing a DuckDB bump and releasing it, when the latest release would pair the new `go.mod` with the old bundle.
- `build-and-release.yml` calls it after the build, for tag pushes and branch dispatches. It sets `BUNDLE_TARBALL` to the run's artifact, so the README's `make download-libs` extracts the bundle just built. The release job doesn't wait for this check, so a DuckDB release can ship before a matching `duckdb-go` exists. If the old `go.mod` doesn't work with the new bundle, this check fails, and so do later PR checks until `go.mod` is bumped.

The job pulls three images with no retries. From ECR Public it pulls `amazonlinux:2023` for the build and `lambda/provided:al2023`, which `sam local invoke` builds its runtime image on. From Docker Hub it pulls `duckdb/duckdb` anonymously, in the Dockerfile's `data` stage. A registry timeout fails the job: `Get "https://public.ecr.aws/v2/": context deadline exceeded` happened once during `sam local invoke`. If a job failed while pulling, re-run it before suspecting the change. No Docker Hub rate-limit errors have shown up so far. If they do, the job needs Docker Hub credentials.

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
