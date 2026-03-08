ARTIFACTS_DIR ?= .aws-sam/build
# Default Go version, can be overridden via command line
GO_VERSION ?= 1.25.5
# Target architecture (override via command line, e.g. make ARCH=amd64)
ARCH ?= arm64
# Bundle type: parquet (default) or lance
BUNDLE ?= lance

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

# Map BUNDLE to asset name and extra CGO libs
ifeq ($(BUNDLE),lance)
	ASSET_NAME := libduckdb_bundle_lance-$(ARCH)
	CGO_EXTRA_LDFLAGS := -llance_duckdb_ffi
else
	ASSET_NAME := libduckdb_bundle-$(ARCH)
	CGO_EXTRA_LDFLAGS :=
endif

.PHONY: clean download-libduckdb_bundle build-DuckDBExampleFunction

build-DuckDBExampleFunction:
	@echo "Building with Docker (ARCH=$(ARCH), BUNDLE=$(BUNDLE))..."
	@echo "Using Go Version: $(GO_VERSION)"
	@mkdir -p $(ARTIFACTS_DIR)
	@docker build \
	--progress=plain \
	--platform $(DOCKER_PLATFORM) \
	--build-arg GO_VERSION=$(GO_VERSION) \
	--build-arg GO_ARCH=$(GO_DOWNLOAD_ARCH) \
	--build-arg CGO_EXTRA_LDFLAGS="$(CGO_EXTRA_LDFLAGS)" \
	--output type=local,dest=$(ARTIFACTS_DIR) \
	--target exporter \
	.
	@echo "Build complete. Artifacts are in $(ARTIFACTS_DIR)/"

# download-libduckdb_bundle
# Downloads the matching release asset from the duckdb-static GitHub releases.
# Use BUNDLE=lance for the lance bundle, or BUNDLE=parquet (default) for parquet.
# Set FORCE=1 to re-download and overwrite existing files.
download-libduckdb_bundle:
	@echo "Preparing ./libs for ARCH=$(ARCH) BUNDLE=$(BUNDLE)"
	@mkdir -p libs
	@if [ -n "$(FORCE)" -a "$(FORCE)" = "1" ]; then rm -rf libs/* || true; fi
	@echo "Querying latest release for Lychee-Technology/duckdb-static..."
	@url=$$(curl -s https://api.github.com/repos/Lychee-Technology/duckdb-static/releases/latest | jq -r '.assets[] | select(.name | test("$(ASSET_NAME)"; "i")) | .browser_download_url' | head -n1); \
	if [ -z "$$url" ]; then echo "No matching asset found for ASSET_NAME=$(ASSET_NAME)"; exit 1; fi; \
	echo "Found $$url"; \
	if [ -f ./libs/libduckdb_bundle_url.txt ] && [ -f ./libs/libduckdb_bundle.a ] && [ "$$(head -1 ./libs/libduckdb_bundle_url.txt)" = "$$url" ] && [ -z "$(FORCE)" ]; then \
		echo "libs already present and url matches; skipping download"; \
	else \
		curl -L -o /tmp/duckdb_asset "$$url" && tar -xJf /tmp/duckdb_asset -C libs && echo "$$url" > libs/libduckdb_bundle_url.txt || (echo "Download or extraction failed"; exit 1); \
	fi; \
	echo "Done."

clean:
	rm -rf $(ARTIFACTS_DIR)
