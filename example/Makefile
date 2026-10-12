ARTIFACTS_DIR ?= .aws-sam/build
# Default Go version, can be overridden via command line
GO_VERSION ?= 1.25.6
# Target architecture (override via command line, e.g. make ARCH=amd64)
# Must match the Architecture parameter in template.yaml (amd64 there is x86_64).
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

# download-libs
# Downloads the matching release asset from the duckdb-static GitHub releases.
# BUNDLE_TARBALL=<path to a bundle .tar.xz> extracts that file instead, such as
# a bundle from a branch run of the build workflow. CI uses it to test the
# example against the bundle it just built.
FLAVOR ?= parquet
ASSET_FLAVOR := httpfs-parquet
LIBS_DIR := libs/$(FLAVOR)
BUNDLE_TARBALL ?=

download-libs:
	@echo "Preparing $(LIBS_DIR) for ARCH=$(ARCH) FLAVOR=$(FLAVOR)"
	@mkdir -p $(LIBS_DIR)
	@if [ -n "$(FORCE)" ] && [ "$(FORCE)" = "1" ]; then rm -rf $(LIBS_DIR)/* || true; fi
ifneq ($(BUNDLE_TARBALL),)
	@echo "Extracting $(BUNDLE_TARBALL)"
	@tar -xJf "$(BUNDLE_TARBALL)" -C $(LIBS_DIR)
	@echo "$(abspath $(BUNDLE_TARBALL))" > $(LIBS_DIR)/url.txt
	@echo "Done."
else
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
endif

clean:
	rm -rf $(ARTIFACTS_DIR)
