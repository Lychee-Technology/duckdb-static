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
	flavor  string
	dataDir string
}

func sqlByFlavor(flavor, dataDir string) string {
	switch flavor {
	case "lance":
		return fmt.Sprintf(`SELECT avg(array_length(tokens)) FROM "%s/imdb_processed.lance"`, dataDir)
	case "vortex":
		return fmt.Sprintf(`SELECT avg(array_length(tokens)) FROM read_vortex("%s/imdb_processed.vortex")`, dataDir)
	default: // parquet
		return fmt.Sprintf(`SELECT avg(array_length(tokens)) FROM read_parquet("%s/imdb_processed.parquet")`, dataDir)
	}
}

func (hc *handlerContext) handler(ctx context.Context, _ any) (string, error) {
	query := sqlByFlavor(hc.flavor, hc.dataDir)
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
	flavor := os.Getenv("FLAVOR")
	if flavor == "" {
		flavor = "parquet"
	}

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

	var extensions []string
	switch flavor {
	case "lance":
		extensions = []string{"lance"}
	case "vortex":
		extensions = []string{"vortex"}
	default: // parquet
		extensions = []string{"parquet"}
	}

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

	fmt.Printf("flavor: %s, dataDir: %s\n", flavor, dataDir)
	hc := &handlerContext{db: db, flavor: flavor, dataDir: dataDir}
	lambda.Start(hc.handler)
}
