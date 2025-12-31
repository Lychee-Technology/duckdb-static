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

func (hc *handlerContext) handler(ctx context.Context, request any) (string, error) {

	s3ObjectURI := os.Getenv("S3_OBJECT_URI")
	if s3ObjectURI == "" {
		return "", fmt.Errorf("S3_OBJECT_URI environment variable is not set")
	}
	result, err := hc.db.QueryContext(ctx, fmt.Sprintf("select count(*) from read_parquet('%s');", s3ObjectURI))

	if err != nil {
		return "", fmt.Errorf("QueryContext failed: %v", err)
	}

	row := result.Next()
	if row {
		var count int
		if err := result.Scan(&count); err != nil {
			return "", fmt.Errorf("scan result failed: %v", err)
		}
		return fmt.Sprintf("count: %d\n", count), nil
	}

	return "", nil
}

func loadExtension(db *sql.DB, extensionNames []string) error {
	begin := time.Now().UnixNano()

	queries := make([]string, len(extensionNames))
	for _, extensionName := range extensionNames {
		queries = append(queries, fmt.Sprintf("LOAD '%s';", extensionName))
	}

	// 执行独立的 SELECT COUNT 作为示例
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

	if err := loadExtension(db, []string{"httpfs"}); err != nil {
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
	handlerContext := &handlerContext{
		db: db,
	}
	handler := handlerContext.handler
	lambda.Start(handler)
}
