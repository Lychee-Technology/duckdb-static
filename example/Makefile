ARTIFACTS_DIR ?= .aws-sam/build
# Default Go version, can be overridden via command line
GO_VERSION ?= 1.25.5
# Target architecture (override via command line, e.g. make ARCH=amd64)
ARCH ?= arm64

export DOCKER_BUILDKIT := 1

# Map ARCH to docker platform and Go download arch
ifeq ($(ARCH),arm64)
	DOCKER_PLATFORM := linux/arm64
	GO_DOWNLOAD_ARCH := arm64
	ASSET_ARCH_PAT := (arm64|aarch64)
else ifeq ($(ARCH),aarch64)
	DOCKER_PLATFORM := linux/arm64
	GO_DOWNLOAD_ARCH := arm64
	ASSET_ARCH_PAT := (arm64|aarch64)
else
	DOCKER_PLATFORM := linux/amd64
	GO_DOWNLOAD_ARCH := amd64
	ASSET_ARCH_PAT := (amd64|x86_64|x64)
endif

.PHONY: build-DuckDBExampleFunction clean download-libduckdb_bundle

# download-libduckdb_bundle
# Downloads the latest release assets from the duckdb-static GitHub releases
# that match the specified ARCH and extracts them into ./libs.
# Set FORCE=1 to re-download and overwrite existing files.
download-libduckdb_bundle:
	@echo "Preparing ./libs for ARCH=$(ARCH)"
	@mkdir -p libs
	@if [ -n "$(FORCE)" -a "$(FORCE)" = "1" ]; then rm -rf libs/* || true; fi
	@echo "Querying latest release for Lychee-Technology/duckdb-static..."
	@url=$$(curl -s https://api.github.com/repos/Lychee-Technology/duckdb-static/releases/latest | jq -r '.assets[] | select(.name | test("$(ASSET_ARCH_PAT)"; "i")) | .browser_download_url' | head -n1); \
	if [ -z "$$url" ]; then echo "No matching asset found for arch $(ARCH)"; exit 1; fi; \
	echo "Found $$url"; \
	if [ -f ./libs/libduckdb_bundle_url.txt ] && [ -f ./libs/libduckdb_bundle.a ] && [ "$$(head -1 ./libs/libduckdb_bundle_url.txt)" = "$$url" ] && [ -z "$(FORCE)" ]; then \
		echo "libs already present and from_url matches; skipping download"; \
	else \
		curl -L -o /tmp/duckdb_asset "$$url" && (tar -xzf /tmp/duckdb_asset -C libs 2>/dev/null || unzip -o /tmp/duckdb_asset -d libs) && echo "$$url" > libs/libduckdb_bundle_url.txt || (echo "Download or extraction failed"; exit 1); \
	fi; \
	echo "Done."

build-DuckDBExampleFunction: download-libduckdb_bundle
	@echo "Building with Docker (ARCH=$(ARCH))..."
	@echo "Using Go Version: $(GO_VERSION)"
	@mkdir -p $(ARTIFACTS_DIR)
	@docker build \
	--platform $(DOCKER_PLATFORM) \
	--build-arg GO_VERSION=$(GO_VERSION) \
	--build-arg GO_ARCH=$(GO_DOWNLOAD_ARCH) \
	--output type=local,dest=$(ARTIFACTS_DIR) \
	--target exporter \
	.
	@echo "Build complete. Artifacts are in $(ARTIFACTS_DIR)/"

clean:
	rm -rf $(ARTIFACTS_DIR)
