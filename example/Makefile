ARTIFACTS_DIR ?= .aws-sam/build
# Default Go version, can be overridden via command line
GO_VERSION ?= 1.25.6
# Target architecture (override via command line, e.g. make ARCH=amd64)
ARCH ?= arm64

export DOCKER_BUILDKIT := 1

# Map ARCH to docker platform and Go download arch
ifeq ($(ARCH),arm64)
	DOCKER_PLATFORM := linux/arm64
	GO_DOWNLOAD_ARCH := arm64
else ifeq ($(ARCH),aarch64)
	DOCKER_PLATFORM := linux/arm64
	GO_DOWNLOAD_ARCH := arm64
	ARCH := arm64
else
	DOCKER_PLATFORM := linux/amd64
	GO_DOWNLOAD_ARCH := amd64
endif

.PHONY: clean \
	build-DuckDBParquetFunction \
	build-DuckDBLanceFunction \
	build-DuckDBVortexFunction \
	download-libs

build-DuckDBParquetFunction:
	@echo "Building DuckDBParquetFunction (ARCH=$(ARCH))..."
	@echo "Using Go Version: $(GO_VERSION)"
	@mkdir -p $(ARTIFACTS_DIR)
	@docker build \
		--progress=plain \
		--platform $(DOCKER_PLATFORM) \
		--build-arg GO_VERSION=$(GO_VERSION) \
		--build-arg GO_ARCH=$(GO_DOWNLOAD_ARCH) \
		--build-arg LIBS_DIR=libs/parquet \
		--output type=local,dest=$(ARTIFACTS_DIR) \
		--target exporter \
		.
	@echo "Build complete. Artifacts are in $(ARTIFACTS_DIR)/"

build-DuckDBLanceFunction:
	@echo "Building DuckDBLanceFunction (ARCH=$(ARCH))..."
	@echo "Using Go Version: $(GO_VERSION)"
	@mkdir -p $(ARTIFACTS_DIR)
	@docker build \
		--progress=plain \
		--platform $(DOCKER_PLATFORM) \
		--build-arg GO_VERSION=$(GO_VERSION) \
		--build-arg GO_ARCH=$(GO_DOWNLOAD_ARCH) \
		--build-arg LIBS_DIR=libs/lance \
		--build-arg CGO_EXTRA_LDFLAGS="-llance_duckdb_ffi" \
		--output type=local,dest=$(ARTIFACTS_DIR) \
		--target exporter \
		.
	@echo "Build complete. Artifacts are in $(ARTIFACTS_DIR)/"

build-DuckDBVortexFunction:
	@echo "Building DuckDBVortexFunction (ARCH=$(ARCH))..."
	@echo "Using Go Version: $(GO_VERSION)"
	@mkdir -p $(ARTIFACTS_DIR)
	@docker build \
		--progress=plain \
		--platform $(DOCKER_PLATFORM) \
		--build-arg GO_VERSION=$(GO_VERSION) \
		--build-arg GO_ARCH=$(GO_DOWNLOAD_ARCH) \
		--build-arg LIBS_DIR=libs/vortex \
		--build-arg CGO_EXTRA_LDFLAGS="-lvortex_duckdb" \
		--output type=local,dest=$(ARTIFACTS_DIR) \
		--target exporter \
		.
	@echo "Build complete. Artifacts are in $(ARTIFACTS_DIR)/"

# download-libs
# Downloads the matching release asset from the duckdb-static GitHub releases.
# Use FLAVOR=parquet|lance|vortex (default: parquet). Set FORCE=1 to re-download.
FLAVOR ?= parquet

ifeq ($(FLAVOR),lance)
	ASSET_FLAVOR := httpfs-parquet-lance
else ifeq ($(FLAVOR),vortex)
	ASSET_FLAVOR := httpfs-parquet-vortex
else
	ASSET_FLAVOR := httpfs-parquet
endif

LIBS_DIR := libs/$(FLAVOR)

download-libs:
	@echo "Preparing $(LIBS_DIR) for ARCH=$(ARCH) FLAVOR=$(FLAVOR)"
	@mkdir -p $(LIBS_DIR)
	@if [ -n "$(FORCE)" ] && [ "$(FORCE)" = "1" ]; then rm -rf $(LIBS_DIR)/* || true; fi
	@echo "Querying latest release for Lychee-Technology/duckdb-static..."
	@PATTERN="libduckdb_bundle-$(ARCH)-linux-$(ASSET_FLAVOR)\\.tar"; \
	url=$$(curl -s https://api.github.com/repos/Lychee-Technology/duckdb-static/releases/latest | \
		jq -r --arg p "$$PATTERN" '.assets[] | select(.name | test($$p; "i")) | .browser_download_url' | head -n1); \
	if [ -z "$$url" ]; then echo "No matching asset found for FLAVOR=$(FLAVOR) ARCH=$(ARCH)"; exit 1; fi; \
	echo "Found $$url"; \
	url_file=$(LIBS_DIR)/url.txt; \
	if [ -f "$$url_file" ] && [ "$$(cat $$url_file)" = "$$url" ] && [ -z "$(FORCE)" ]; then \
		echo "$(LIBS_DIR) already present and url matches; skipping download"; \
	else \
		curl -L -o /tmp/duckdb_asset "$$url" && \
		tar -xJf /tmp/duckdb_asset -C $(LIBS_DIR) && \
		echo "$$url" > "$$url_file" || (echo "Download or extraction failed"; exit 1); \
	fi; \
	echo "Done."

clean:
	rm -rf $(ARTIFACTS_DIR)
