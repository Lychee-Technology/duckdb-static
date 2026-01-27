# syntax=docker/dockerfile:1

# --------------------------------------------------------
# Stage 1: Builder
# Prepares the environment, downloads dependencies, and builds the binary.
# --------------------------------------------------------
FROM public.ecr.aws/amazonlinux/amazonlinux:2023 AS builder

ARG GO_VERSION=1.25.6
ARG GOOS=linux
ARG GO_ARCH=arm64

# 1. Install Toolchain and Dependencies
RUN dnf -y update && \
    dnf -y install --allowerasing \
      curl tar gzip zip git make which \
      gcc14 gcc14-c++ \
      openssl-devel zlib-devel libcurl-devel \
      gcc14-libstdc++-devel && \
    dnf clean all && \
    rm -rf /var/cache/dnf

# 2. Install Go (by ARCH)
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${GO_ARCH}.tar.gz" | tar -C /usr/local -xz

ENV PATH="/usr/local/go/bin:${PATH}"

# 3. Set Build Environment Variables
ENV GOOS=${GOOS} \
    GOARCH=${GO_ARCH} \
    CGO_ENABLED=1 \
    CC=/usr/bin/gcc14-gcc \
    CXX=/usr/bin/gcc14-g++ \
    GOCACHE=/root/.cache/go-build \
    GOMODCACHE=/go/pkg/mod

# DuckDB specific flags
# Note: -L/src/libs assumes you have a 'libs' folder in your project root containing the `duckdb_bundle.a` static library.
ENV CPPFLAGS="-DDUCKDB_STATIC_BUILD" \
    CGO_LDFLAGS="-L/src/libs -lduckdb_bundle -lstdc++ -lm -lcurl -lssl -lcrypto -lpthread"

WORKDIR /src

# 4. Dependency Caching Layer
# Copy only the dependency definition files first.
# This ensures 'go mod download' is cached unless go.mod/go.sum changes.
COPY go.mod go.sum ./

# Download modules using a persistent cache mount.
RUN --mount=type=cache,target=/go/pkg/mod \
    go mod download

# 5. Copy Source Code
COPY . .

# 6. Build the Binary
# We output the binary to a separate /out directory for easier export.
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    mkdir -p /out && \
    go build \
      -tags=duckdb_use_static_lib \
      -ldflags="-s -w" \
      -o /out/bootstrap \
      ./cmd

# 7. Package the Artifact
WORKDIR /out
RUN zip -9 bootstrap.zip bootstrap

# --------------------------------------------------------
# Stage 2: Exporter
# This stage is used solely to export the artifacts back to the host machine.
# --------------------------------------------------------
FROM scratch AS exporter

COPY --from=builder /out/bootstrap /
COPY --from=builder /out/bootstrap.zip /
