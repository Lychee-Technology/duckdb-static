// Command duckdb-bench measures, inside a fresh Lambda process, how long DuckDB
// takes to open a database and connect, make the bundled extensions ready, and
// run a first query on a small Parquet file over HTTP, from the fixture server
// run.sh starts next to the Lambda containers. run.sh builds it twice, against
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
	"database/sql"
	"encoding/hex"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/duckdb/duckdb-go/v2"
)

// The bundle's extensions. DuckDB 1.5.6 loads the extensions linked into the
// executable when it opens a database, so LOAD does nothing more for them,
// except for httpfs in the stock variant, which LOAD reads from the package's
// ext directory.
var extensions = []string{"core_functions", "parquet", "json", "icu", "httpfs"}

const (
	// sum(id) of test/fixtures/public/v2/tiny.parquet, which the benchmark
	// reads from BENCH_FIXTURE_URL (test/fixtures/v2.queries.sql).
	expectedSum = 720600
	// sum(id) of v1/multi_row_group.parquet, which mode "remote" reads over
	// HTTPS (test/fixtures/v1.queries.sql).
	remoteExpectedSum = 1249975000
)

type event struct {
	// "bench" (the default) or "remote".
	Mode string `json:"mode"`
	// For "remote": a URL of v1/multi_row_group.parquet to query, untimed.
	URL string `json:"url"`
}

type extensionState struct {
	Name        string `json:"name"`
	Loaded      bool   `json:"loaded"`
	Installed   bool   `json:"installed"`
	InstallMode string `json:"install_mode"`
	InstallPath string `json:"install_path"`
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
	FirstQueryMs     *float64 `json:"first_http_parquet_query_ms,omitempty"`
	TotalMs          *float64 `json:"duckdb_ready_and_first_http_query_ms,omitempty"`

	// The URL the query read. In mode "bench" it is BENCH_FIXTURE_URL with
	// this process's instance ID added as the query parameter "instance", so
	// that the fixture server's log tells which sample made each request.
	URL      string `json:"url"`
	Result   *int64 `json:"result"`
	ResultOK bool   `json:"result_ok"`
	Error    string `json:"error"`

	// Provenance, collected after the timed interval.
	LibraryVersion  string            `json:"library_version"`
	SourceID        string            `json:"source_id"`
	Platform        string            `json:"platform"`
	Extensions      []extensionState  `json:"extensions"`
	Settings        map[string]string `json:"settings"`
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

// querySum reads every value of the id column from src and checks the sum.
func querySum(ctx context.Context, conn *sql.Conn, src string, expected int64, r *result) error {
	var sum int64
	query := fmt.Sprintf("SELECT sum(id) FROM read_parquet('%s')", strings.ReplaceAll(src, "'", "''"))
	if err := conn.QueryRowContext(ctx, query).Scan(&sum); err != nil {
		return fmt.Errorf("query: %w", err)
	}
	r.Result = &sum
	r.ResultOK = sum == expected
	return nil
}

// sampleURL adds the process's instance ID to the fixture's URL.
func sampleURL(base, instanceID string) (string, error) {
	u, err := url.Parse(base)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return "", fmt.Errorf("BENCH_FIXTURE_URL %q isn't an http:// or https:// URL", base)
	}
	q := u.Query()
	q.Set("instance", instanceID)
	u.RawQuery = q.Encode()
	return u.String(), nil
}

// bench times sql.Open's work, the first connection, the LOADs and the first
// query, including reading and checking its result, separately. sql.Open
// would open the database at once but connect lazily, so it calls the
// connector and the connection explicitly. The URL is worked out before the
// clock starts.
func bench(ctx context.Context, r *result) error {
	src, err := sampleURL(os.Getenv("BENCH_FIXTURE_URL"), r.InstanceID)
	if err != nil {
		return err
	}
	r.URL = src
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
	if err := querySum(ctx, conn, src, expectedSum, r); err != nil {
		return err
	}
	done := time.Now()

	r.OpenOrConnectMs = ms(opened.Sub(start))
	r.ExtensionReadyMs = ms(loaded.Sub(opened))
	r.FirstQueryMs = ms(done.Sub(loaded))
	r.TotalMs = ms(done.Sub(start))

	return describe(ctx, conn, r)
}

// remote checks that httpfs reads the fixture from src. It is a functional
// check outside the benchmark, so nothing in it is timed.
func remote(ctx context.Context, r *result, src string) error {
	if src == "" {
		return fmt.Errorf("mode remote needs a url")
	}
	r.URL = src
	db, conn, err := open(ctx)
	if err != nil {
		return err
	}
	defer db.Close()
	defer conn.Close()
	if err := load(ctx, conn); err != nil {
		return err
	}
	if err := querySum(ctx, conn, src, remoteExpectedSum, r); err != nil {
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
	// Also every other loaded extension: the stock library's open loads the
	// extensions it links beyond the five.
	rows, err := conn.QueryContext(ctx, `SELECT extension_name, loaded, installed,
			coalesce(install_mode::VARCHAR, ''), coalesce(install_path, '')
		FROM duckdb_extensions() WHERE loaded OR extension_name IN (`+strings.Join(names, ", ")+`) ORDER BY extension_name`)
	if err != nil {
		return fmt.Errorf("extensions: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var e extensionState
		if err := rows.Scan(&e.Name, &e.Loaded, &e.Installed, &e.InstallMode, &e.InstallPath); err != nil {
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
