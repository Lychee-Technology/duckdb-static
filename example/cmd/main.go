package main

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	_ "github.com/duckdb/duckdb-go/v2"
)

type handlerContext struct {
	db *sql.DB
}

func getEnvWithDefault(key, defaultValue string) string {
	value := os.Getenv(key)
	if value == "" {
		return defaultValue
	}
	return value
}

// parquetHandler queries a Parquet file from S3 and returns the row count.
func (hc *handlerContext) parquetHandler(ctx context.Context, request any) (string, error) {
	s3ObjectURI := os.Getenv("S3_OBJECT_URI")
	if s3ObjectURI == "" {
		return "", fmt.Errorf("S3_OBJECT_URI environment variable is not set")
	}
	rows, err := hc.db.QueryContext(ctx, fmt.Sprintf("SELECT count(*) FROM read_parquet('%s');", s3ObjectURI))
	if err != nil {
		return "", fmt.Errorf("QueryContext failed: %v", err)
	}
	defer rows.Close()
	if rows.Next() {
		var count int
		if err := rows.Scan(&count); err != nil {
			return "", fmt.Errorf("scan result failed: %v", err)
		}
		return fmt.Sprintf("count: %d\n", count), nil
	}
	return "", nil
}

// lanceHandler writes a small Lance dataset to /tmp, reads it back, and
// optionally queries a Lance dataset from S3 if S3_OBJECT_URI is set.
func (hc *handlerContext) lanceHandler(ctx context.Context, request any) (string, error) {
	const lanceDir = "/tmp/test_lance"

	// Write a small Lance dataset
	_, err := hc.db.ExecContext(ctx, fmt.Sprintf(
		`COPY (
			SELECT i AS id,
			       [CAST(i AS FLOAT), CAST(i*2 AS FLOAT), CAST(i*3 AS FLOAT)] AS vector,
			       'item_' || CAST(i AS VARCHAR) AS label
			FROM range(10) t(i)
		) TO '%s' (FORMAT lance)`, lanceDir))
	if err != nil {
		return "", fmt.Errorf("lance write failed: %v", err)
	}

	// Read it back
	rows, err := hc.db.QueryContext(ctx, fmt.Sprintf(
		`SELECT id, label FROM lance_scan('%s') ORDER BY id LIMIT 5`, lanceDir))
	if err != nil {
		return "", fmt.Errorf("lance_scan failed: %v", err)
	}
	defer rows.Close()

	var result strings.Builder
	result.WriteString("lance local test:\n")
	for rows.Next() {
		var id int
		var label string
		if err := rows.Scan(&id, &label); err != nil {
			return "", fmt.Errorf("scan failed: %v", err)
		}
		result.WriteString(fmt.Sprintf("  id=%d label=%s\n", id, label))
	}

	// If S3_OBJECT_URI is set, also query a Lance dataset from S3
	s3URI := os.Getenv("S3_OBJECT_URI")
	if s3URI != "" {
		countRows, err := hc.db.QueryContext(ctx,
			fmt.Sprintf("SELECT count(*) FROM lance_scan('%s');", s3URI))
		if err != nil {
			return "", fmt.Errorf("S3 lance_scan failed: %v", err)
		}
		defer countRows.Close()
		if countRows.Next() {
			var count int
			if err := countRows.Scan(&count); err != nil {
				return "", fmt.Errorf("scan count failed: %v", err)
			}
			result.WriteString(fmt.Sprintf("s3 lance count: %d\n", count))
		}
	}

	return result.String(), nil
}

func loadExtension(db *sql.DB, extensionNames []string) error {
	begin := time.Now().UnixNano()

	queries := make([]string, 0, len(extensionNames))
	for _, extensionName := range extensionNames {
		queries = append(queries, fmt.Sprintf("LOAD '%s';", extensionName))
	}

	if _, err := db.Exec(strings.Join(queries, "\n")); err != nil {
		return err
	}

	end := time.Now().UnixNano()
	fmt.Printf("load extensions took %d ms\n", (end-begin)/1e6)

	return nil
}

func main() {
	db, err := sql.Open("duckdb", ":memory:")
	if err != nil {
		fmt.Printf("open duckdb failed: %v", err)
		return
	}
	db.SetMaxOpenConns(100)
	db.SetMaxIdleConns(10)
	db.SetConnMaxLifetime(time.Minute * 5)
	db.SetConnMaxIdleTime(time.Minute * 2)
	defer db.Close()

	if err := loadExtension(db, []string{"httpfs", "lance"}); err != nil {
		fmt.Printf("loadExtension failed: %v", err)
		return
	}

	begin := time.Now().UnixNano()
	_, err = db.Exec(`CREATE OR REPLACE SECRET secret (
		TYPE S3,
		PROVIDER config,
		ENDPOINT ?,
		KEY_ID ?,
		SECRET ?,
		SESSION_TOKEN ?,
		REGION ?,
		USE_SSL ?,
		URL_STYLE ?
	);`,
		os.Getenv("S3_ENDPOINT"),
		os.Getenv("AWS_ACCESS_KEY_ID"),
		os.Getenv("AWS_SECRET_ACCESS_KEY"),
		os.Getenv("AWS_SESSION_TOKEN"),
		os.Getenv("AWS_REGION"),
		getEnvWithDefault("S3_USE_SSL", "true"),
		getEnvWithDefault("S3_URL_STYLE", "vhost"),
	)
	end := time.Now().UnixNano()
	fmt.Printf("CREATE secret took %d ms\n", (end-begin)/1e6)
	if err != nil {
		fmt.Printf("CREATE secret failed: %v", err)
		return
	}

	hc := &handlerContext{db: db}

	// Select handler based on BUNDLE_TYPE env var ("lance" or "parquet")
	bundleType := getEnvWithDefault("BUNDLE_TYPE", "lance")
	fmt.Printf("bundle type: %s\n", bundleType)

	if bundleType == "lance" {
		lambda.Start(hc.lanceHandler)
	} else {
		lambda.Start(hc.parquetHandler)
	}
}
