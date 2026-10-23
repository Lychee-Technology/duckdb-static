# syntax=docker/dockerfile:1

# Builds duckdb-bench twice, against duckdb-go's stock prebuilt library and
# against this repository's bundle, plus the launcher both packages use as
# their bootstrap. The two builds share every setting except the build tag
# and the bundle's CPPFLAGS and CGO_LDFLAGS. The build context is example/, so
# both use example/go.mod's duckdb-go. run.sh puts the bundle in bench/libs/.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023 AS builder

ARG GO_VERSION=1.25.6
ARG GO_ARCH=arm64

RUN dnf -y update && \
    dnf -y install --allowerasing \
      curl tar gzip which \
      gcc14 gcc14-c++ \
      openssl-devel zlib-devel libcurl-devel \
      gcc14-libstdc++-devel && \
    dnf clean all && \
    rm -rf /var/cache/dnf

RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${GO_ARCH}.tar.gz" | tar -C /usr/local -xz

ENV PATH="/usr/local/go/bin:${PATH}" \
    GOOS=linux \
    GOARCH=${GO_ARCH} \
    CGO_ENABLED=1 \
    CC=/usr/bin/gcc14-gcc \
    CXX=/usr/bin/gcc14-g++ \
    GOFLAGS=-buildvcs=false \
    GOCACHE=/root/.cache/go-build \
    GOMODCACHE=/go/pkg/mod

WORKDIR /src
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod \
    go mod download
COPY bench/ ./bench/

RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    mkdir -p /out/stock /out/custom && \
    go build -ldflags="-s -w" -o /out/stock/duckdb-bench ./bench && \
    CPPFLAGS="-DDUCKDB_STATIC_BUILD" \
    CGO_LDFLAGS="-L/src/bench/libs -lduckdb_bundle -lstdc++ -lm -lcurl -lssl -lcrypto -lpthread -ldl" \
      go build -tags=duckdb_use_static_lib -ldflags="-s -w" -o /out/custom/duckdb-bench ./bench && \
    gcc14-gcc -O2 -o /out/bootstrap bench/launcher/launcher.c && \
    { echo "go: $(go version)"; \
      echo "cc: $(gcc14-gcc --version | head -n1)"; \
      echo "go build flags: -ldflags=-s -w, GOFLAGS=$GOFLAGS"; \
      echo "stock: no build tag; duckdb-go-bindings' prebuilt go.duckdb.org/libduckdb library"; \
      echo "custom: -tags=duckdb_use_static_lib; CPPFLAGS=-DDUCKDB_STATIC_BUILD; CGO_LDFLAGS=-L/src/bench/libs -lduckdb_bundle -lstdc++ -lm -lcurl -lssl -lcrypto -lpthread -ldl"; \
      echo "libduckdb module: $(go list -m -f '{{.Path}} {{.Version}}' all | grep '^go.duckdb.org/libduckdb/linux-' | grep -F "$GO_ARCH")"; \
    } > /out/build-info.txt

FROM scratch AS exporter
COPY --from=builder /out /
