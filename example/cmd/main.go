package main

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	_ "github.com/duckdb/duckdb-go/v2"
)

type handlerContext struct {
	db      *sql.DB
	dataDir string
}

func sqlByFlavor(dataDir string) string {
	return fmt.Sprintf(`SELECT avg(array_length(tokens)) FROM read_parquet("%s/imdb_processed.parquet")`, dataDir)
}

func (hc *handlerContext) handler(ctx context.Context, _ any) (string, error) {
	query := sqlByFlavor(hc.dataDir)
	rows, err := hc.db.QueryContext(ctx, query)
	if err != nil {
		return "", fmt.Errorf("query failed: %v", err)
	}
	defer rows.Close()

	if rows.Next() {
		var avg float64
		if err := rows.Scan(&avg); err != nil {
			return "", fmt.Errorf("scan failed: %v", err)
		}
		return fmt.Sprintf("avg(array_length(tokens)): %f\n", avg), nil
	}
	return "", nil
}

func loadExtensions(db *sql.DB, names []string) error {
	begin := time.Now().UnixNano()

	queries := make([]string, 0, len(names))
	for _, name := range names {
		queries = append(queries, fmt.Sprintf("LOAD '%s';", name))
	}

	if _, err := db.Exec(strings.Join(queries, "\n")); err != nil {
		return err
	}

	fmt.Printf("load extensions took %d ms\n", (time.Now().UnixNano()-begin)/1e6)
	return nil
}

func main() {
	db, err := sql.Open("duckdb", ":memory:")
	if err != nil {
		fmt.Printf("open duckdb failed: %v\n", err)
		return
	}
	db.SetMaxOpenConns(100)
	db.SetMaxIdleConns(10)
	db.SetConnMaxLifetime(time.Minute * 5)
	db.SetConnMaxIdleTime(time.Minute * 2)
	defer db.Close()

	extensions := []string{"parquet"}

	if err := loadExtensions(db, extensions); err != nil {
		fmt.Printf("loadExtensions failed: %v\n", err)
		return
	}

	wd, err := os.Getwd()
	if err != nil {
		fmt.Printf("getwd failed: %v\n", err)
		return
	}
	dataDir := filepath.Join(wd, "data")

	fmt.Printf("dataDir: %s\n", dataDir)
	hc := &handlerContext{db: db, dataDir: dataDir}
	lambda.Start(hc.handler)
}
