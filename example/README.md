# DuckDB Static Linking Example for AWS Lambda

This example demonstrates how to build a Go-based AWS Lambda function that uses DuckDB and some of its extensions with static linking. This approach ensures that all necessary DuckDB libraries are bundled into the Lambda deployment package, avoiding issues with missing shared libraries in the Lambda environment.

## Project Structure

```bash
.
├── Dockerfile          # Multi-stage Docker build for the Lambda binary
├── Makefile            # Automates downloading DuckDB libs and building
├── cmd/
│   └── main.go         # Lambda function code using DuckDB
├── libs/               # (Generated) Directory for static DuckDB libraries
├── template.yaml       # AWS SAM template
└── samconfig.toml      # AWS SAM configuration
```

## How it Works

1.  **Static Libraries**: The `Makefile` downloads pre-compiled static DuckDB libraries from the [duckdb-static](https://github.com/Lychee-Technology/duckdb-static) repository.
2.  **Docker Build**: A multi-stage `Dockerfile` is used to build the Go binary. It uses `amazonlinux:2023` to match the Lambda `provided.al2023` runtime.
3.  **CGO Linking**: The build process uses `CGO_LDFLAGS` to link against the static libraries in the `libs/` directory.
4.  **Lambda Function**: The function loads the `httpfs` extension and can query Parquet files directly from S3.

## Requirements

* [Docker](https://www.docker.com/community-edition) installed
* [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/serverless-sam-cli-install.html)
* `jq` and `curl` (for downloading libraries)
* [Golang](https://golang.org) (optional, as build happens in Docker)

## Setup and Build

### 1. Download DuckDB Static Libraries

Run the following command to download the static libraries for your target architecture (default is `arm64`):

```bash
make download-libs
```

This will populate the `libs/` directory with `duckdb_bundle.a` and other necessary files.

### 2. Build the Lambda Function

You can build the function using SAM, which will invoke the `Makefile`:

```bash
sam build
```

> **Note**: The `Makefile` target `build-DuckDBExampleFunction` (or similar) must match the resource name in `template.yaml`.

## Local Development

To invoke the function locally, you need to provide the required environment variables. You can use an `env.json` file.

Then run:

```bash
sam local invoke DuckDBExampleFunction --env-vars env.json
```

## Deployment

To deploy to AWS:

```bash
sam deploy --guided
```

## Lambda Function Details

The example function in `cmd/main.go`:
- Opens an in-memory DuckDB database.
- Loads the `httpfs` extension.
- Configures S3 credentials using environment variables.
- Executes a `SELECT count(*) FROM read_parquet(...)` query on an S3 object.
