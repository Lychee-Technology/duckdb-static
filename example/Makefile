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
# Downloads the matching asset of the newest release tagged $(DUCKDB_VER)_<n>
# from the duckdb-static GitHub releases (release-url.sh picks it), not the
# release marked Latest, which is main's DuckDB 2.0 build.
# BUNDLE_TARBALL=<path to a bundle .tar.xz> extracts that file instead, such as
# a bundle from a branch run of the build workflow. CI uses it to test the
# example against the bundle it just built.
FLAVOR ?= parquet
ASSET_FLAVOR := httpfs-parquet
LIBS_DIR := libs/$(FLAVOR)
BUNDLE_TARBALL ?=
# The DuckDB version whose release download-libs fetches: the repository's
# duckdb_version, which go.mod's duckdb-go versions must match.
DUCKDB_VER ?= $(shell sed -n 's/^DUCKDB_VER=//p' ../duckdb_version 2>/dev/null)

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
	@if [ -z "$(DUCKDB_VER)" ]; then echo "No DuckDB version: ../duckdb_version has no DUCKDB_VER. Name the version, such as make download-libs DUCKDB_VER=v1.5.6."; exit 1; fi
	@echo "Querying the newest $(DUCKDB_VER)_<n> release of Lychee-Technology/duckdb-static..."
	@url=$$(./release-url.sh "$(DUCKDB_VER)" "$(ARCH)" "$(ASSET_FLAVOR)") || exit 1; \
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
