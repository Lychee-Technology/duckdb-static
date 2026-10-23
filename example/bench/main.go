// Command duckdb-bench measures, inside a fresh Lambda process, how long DuckDB
// takes to open a database and connect, make the bundled extensions ready, and
// run a first query on a local Parquet file. run.sh builds it twice, against
// the stock duckdb-go library and against this repository's bundle, and
// README.md says what each number covers.
package main

/*
#include <time.h>

static long long monotonic_ns(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}
*/
import "C"

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/duckdb/duckdb-go/v2"
)

// The bundle's extensions, in its registration order. The stock variant loads
// httpfs from the package's ext directory and has the others built in.
var extensions = []string{"core_functions", "parquet", "json", "icu", "httpfs"}

const (
	fixture     = "data/multi_row_group.parquet"
	expectedSum = 1249975000 // test/fixtures/v1.queries.sql
)

type event struct {
	// "bench" (the default) or "remote".
	Mode string `json:"mode"`
	// For "remote": a URL of multi_row_group.parquet to query, untimed.
	URL string `json:"url"`
}

type extensionState struct {
	Name        string `json:"name"`
	Loaded      bool   `json:"loaded"`
	Installed   bool   `json:"installed"`
	InstallMode string `json:"install_mode"`
}

type result struct {
	Mode       string `json:"mode"`
	Variant    string `json:"variant"`
	InstanceID string `json:"instance_id"`
	Invocation int    `json:"invocation"`
	PID        int    `json:"pid"`

	// Secondary: from the launcher's exec to the first statement of main.
	BootstrapToMainMs *float64 `json:"bootstrap_to_main_ms"`

	// Primary, mode "bench" only.
	OpenOrConnectMs  *float64 `json:"duckdb_open_or_connect_ms,omitempty"`
	ExtensionReadyMs *float64 `json:"extension_ready_ms,omitempty"`
	FirstQueryMs     *float64 `json:"first_local_parquet_query_ms,omitempty"`
	TotalMs          *float64 `json:"duckdb_ready_and_first_local_query_ms,omitempty"`

	Result   *int64 `json:"result"`
	ResultOK bool   `json:"result_ok"`
	Error    string `json:"error"`

	// Provenance, collected after the timed interval.
	LibraryVersion  string            `json:"library_version"`
	SourceID        string            `json:"source_id"`
	Platform        string            `json:"platform"`
	Extensions      []extensionState  `json:"extensions"`
	Settings        map[string]string `json:"settings"`
	FixtureSHA256   string            `json:"fixture_sha256,omitempty"`
	FixtureBytes    int64             `json:"fixture_bytes,omitempty"`
	ExecutableBytes int64             `json:"executable_bytes"`
}

type process struct {
	instanceID        string
	bootstrapToMainMs *float64
	invocations       int
}

func main() {
	mainNs := int64(C.monotonic_ns())
	p := &process{instanceID: newInstanceID()}
	// The launcher, bootstrap, reads the same clock just before it execs this
	// program. Without it, the metric is missing and the sample is rejected.
	if s := os.Getenv("BENCH_BOOTSTRAP_MONOTONIC_NS"); s != "" {
		if ns, err := strconv.ParseInt(s, 10, 64); err == nil && mainNs >= ns {
			v := float64(mainNs-ns) / 1e6
			p.bootstrapToMainMs = &v
		}
	}
	lambda.Start(p.handle)
}

func newInstanceID() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}

func taskRoot() string {
	if root := os.Getenv("LAMBDA_TASK_ROOT"); root != "" {
		return root
	}
	return "/var/task"
}

// Both variants open the database with the same configuration. Nothing may be
// installed or loaded behind the program's back, so a missing extension fails
// instead of downloading.
func dsn() string {
	q := url.Values{}
	q.Set("autoinstall_known_extensions", "false")
	q.Set("autoload_known_extensions", "false")
	q.Set("extension_directory", filepath.Join(taskRoot(), "ext"))
	return ":memory:?" + q.Encode()
}

func (p *process) handle(ctx context.Context, ev event) (*result, error) {
	p.invocations++
	r := &result{
		Mode:              ev.Mode,
		Variant:           os.Getenv("BENCH_VARIANT"),
		InstanceID:        p.instanceID,
		Invocation:        p.invocations,
		PID:               os.Getpid(),
		BootstrapToMainMs: p.bootstrapToMainMs,
	}
	if r.Mode == "" {
		r.Mode = "bench"
	}
	if exe, err := os.Executable(); err == nil {
		if st, err := os.Stat(exe); err == nil {
			r.ExecutableBytes = st.Size()
		}
	}

	var err error
	switch r.Mode {
	case "bench":
		err = bench(ctx, r)
	case "remote":
		err = remote(ctx, r, ev.URL)
	default:
		err = fmt.Errorf("unknown mode %q", r.Mode)
	}
	if err != nil {
		r.Error = err.Error()
	}
	return r, nil
}

func ms(d time.Duration) *float64 {
	v := float64(d.Nanoseconds()) / 1e6
	return &v
}

func open(ctx context.Context) (*sql.DB, *sql.Conn, error) {
	connector, err := duckdb.NewConnector(dsn(), nil)
	if err != nil {
		return nil, nil, fmt.Errorf("open: %w", err)
	}
	db := sql.OpenDB(connector)
	conn, err := db.Conn(ctx)
	if err != nil {
		db.Close()
		return nil, nil, fmt.Errorf("connect: %w", err)
	}
	return db, conn, nil
}

func load(ctx context.Context, conn *sql.Conn) error {
	for _, name := range extensions {
		if _, err := conn.ExecContext(ctx, "LOAD "+name); err != nil {
			return fmt.Errorf("LOAD %s: %w", name, err)
		}
	}
	return nil
}

func querySum(ctx context.Context, conn *sql.Conn, path string, r *result) error {
	var sum int64
	query := fmt.Sprintf("SELECT sum(id) FROM read_parquet('%s')", strings.ReplaceAll(path, "'", "''"))
	if err := conn.QueryRowContext(ctx, query).Scan(&sum); err != nil {
		return fmt.Errorf("query: %w", err)
	}
	r.Result = &sum
	r.ResultOK = sum == expectedSum
	return nil
}

// bench times sql.Open's work, the first connection, the LOADs and the first
// query separately. sql.Open would open the database at once but connect
// lazily, so it calls the connector and the connection explicitly.
func bench(ctx context.Context, r *result) error {
	start := time.Now()
	db, conn, err := open(ctx)
	if err != nil {
		return err
	}
	defer db.Close()
	defer conn.Close()
	opened := time.Now()
	if err := load(ctx, conn); err != nil {
		return err
	}
	loaded := time.Now()
	path := filepath.Join(taskRoot(), fixture)
	if err := querySum(ctx, conn, path, r); err != nil {
		return err
	}
	done := time.Now()

	r.OpenOrConnectMs = ms(opened.Sub(start))
	r.ExtensionReadyMs = ms(loaded.Sub(opened))
	r.FirstQueryMs = ms(done.Sub(loaded))
	r.TotalMs = ms(done.Sub(start))

	if err := describe(ctx, conn, r); err != nil {
		return err
	}
	sum, size, err := hashFile(path)
	if err != nil {
		return err
	}
	r.FixtureSHA256, r.FixtureBytes = sum, size
	return nil
}

// remote checks that httpfs reads the fixture from src. It is a functional
// check outside the benchmark, so nothing in it is timed.
func remote(ctx context.Context, r *result, src string) error {
	if src == "" {
		return fmt.Errorf("mode remote needs a url")
	}
	db, conn, err := open(ctx)
	if err != nil {
		return err
	}
	defer db.Close()
	defer conn.Close()
	if err := load(ctx, conn); err != nil {
		return err
	}
	if err := querySum(ctx, conn, src, r); err != nil {
		return err
	}
	return describe(ctx, conn, r)
}

func describe(ctx context.Context, conn *sql.Conn, r *result) error {
	if err := conn.QueryRowContext(ctx, "SELECT library_version, source_id FROM pragma_version()").
		Scan(&r.LibraryVersion, &r.SourceID); err != nil {
		return fmt.Errorf("version: %w", err)
	}
	if err := conn.QueryRowContext(ctx, "SELECT platform FROM pragma_platform()").Scan(&r.Platform); err != nil {
		return fmt.Errorf("platform: %w", err)
	}

	names := make([]string, len(extensions))
	for i, name := range extensions {
		names[i] = "'" + name + "'"
	}
	rows, err := conn.QueryContext(ctx, `SELECT extension_name, loaded, installed, coalesce(install_mode::VARCHAR, '')
		FROM duckdb_extensions() WHERE extension_name IN (`+strings.Join(names, ", ")+`) ORDER BY extension_name`)
	if err != nil {
		return fmt.Errorf("extensions: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var e extensionState
		if err := rows.Scan(&e.Name, &e.Loaded, &e.Installed, &e.InstallMode); err != nil {
			return fmt.Errorf("extensions: %w", err)
		}
		r.Extensions = append(r.Extensions, e)
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("extensions: %w", err)
	}

	r.Settings = map[string]string{}
	for _, name := range []string{"autoinstall_known_extensions", "autoload_known_extensions", "extension_directory"} {
		var v string
		if err := conn.QueryRowContext(ctx, "SELECT current_setting('"+name+"')::VARCHAR").Scan(&v); err != nil {
			return fmt.Errorf("setting %s: %w", name, err)
		}
		r.Settings[name] = v
	}
	return nil
}

func hashFile(path string) (string, int64, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(h.Sum(nil)), n, nil
}
