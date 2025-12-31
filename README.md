# DuckDB Static Library

> **A pre-compiled, static bundle of DuckDB optimized for serverless environments (AWS Lambda, Google Cloud Functions, etc.).**


## Why DuckDB Static Library?

Running [DuckDB](https://duckdb.org/) in serverless environments can be challenging due to initialization overhead and dependency management. This library solves these pain points by providing a strictly bundled static build.

### ️ Blazing Fast Cold Starts
**Cold start time is critical.**
When using dynamic loading, initialization can be slow. By using a static bundle, all necessary components are pre-compiled into a single package.
* **Performance Gain:** Reduces initialization time from **>4s** down to **~0.3s**.
* **Result:** Significantly improved responsiveness for Lambda/Cloud Functions relying on DuckDB and its extensions.

### Robust & Portable ("Batteries Included")
Using DuckDB with extensions in ephemeral environments often leads to runtime errors due to missing binaries or version mismatches.
* **Zero Dependencies:** No need to download extensions at runtime.
* **Consistency:** Ensures that all core components are version-matched and guaranteed to work, simplifying deployment across different systems.


## What's Included?

This bundle is built on **Amazon Linux 2023** and comes pre-packaged with the most essential extensions:

| Extension | Description                              |
| :-------- | :--------------------------------------- |
| `json`    | JSON manipulation and querying           |
| `icu`     | International Components for Unicode     |
| `httpfs`  | HTTP file system support (S3, GCS, etc.) |
| `parquet` | Parquet columnar file format support     |


## How It Works

### Automated Build Process
This repository utilizes a **GitHub Actions** workflow to ensure reliability and transparency:
1.  **Trigger:** workflow triggers automatically whenever a new tag is pushed.
2.  **Build:** Compiles a static bundle of DuckDB + Extensions on Amazon Linux 2023.
3.  **Release:** Automatically generates `.tar.xz` files and publishes them to GitHub Releases.

### Installation / Usage
You don't need to build from source. simply:
1.  Go to the [**Releases**](https://github.com/Lychee-Technology/duckdb-static/releases) page.
2.  Download the latest `.tar.xz` file for the corresponding arch.
3.  Include the binary in your serverless deployment package.


## Contributing
Contributions are welcome! Please feel free to submit a Pull Request.


## Disclaimer

This project is an independent open-source initiative and is **not** affiliated with, endorsed by, or associated with the DuckDB Foundation or DuckDB Labs.
**DuckDB** is a trademark of the **DuckDB Foundation**. All other trademarks, logos, and service marks used in this repository are the property of their respective owners.