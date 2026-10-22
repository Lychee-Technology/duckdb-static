-- The exact results the v1 fixtures must give. Each entry is a line
-- "-- expect: <value>" followed by one query that ends with ";" at the end of
-- a line. The query returns one row with one column, and <value> is that
-- column cast to VARCHAR, which is what duckdb_value_varchar returns. {dir} is
-- the directory that holds the fixture files: a local path, or a URL such as
-- https://<site>/v1. test/fixtures/fixtures.py checks every entry when it
-- generates the files.

-- basic.parquet: id 1..100
-- expect: 100
SELECT count(*) FROM read_parquet('{dir}/basic.parquet');
-- expect: INTEGER,VARCHAR,DECIMAL(10,2),BOOLEAN,DATE
SELECT typeof(id) || ',' || typeof(name) || ',' || typeof(price) || ',' || typeof(even) || ',' || typeof(day)
FROM read_parquet('{dir}/basic.parquet') LIMIT 1;
-- expect: 5050
SELECT sum(id) FROM read_parquet('{dir}/basic.parquet');
-- expect: item 42
SELECT name FROM read_parquet('{dir}/basic.parquet') WHERE id = 42;
-- expect: 6312.50
SELECT sum(price) FROM read_parquet('{dir}/basic.parquet');
-- expect: 50
SELECT count(*) FROM read_parquet('{dir}/basic.parquet') WHERE even;
-- expect: 2024-04-10
SELECT max(day) FROM read_parquet('{dir}/basic.parquet');

-- nullable.parquet: id 1..1000
-- expect: 1000
SELECT count(*) FROM read_parquet('{dir}/nullable.parquet');
-- expect: 667
SELECT count(a) FROM read_parquet('{dir}/nullable.parquet');
-- expect: 800
SELECT count(s) FROM read_parquet('{dir}/nullable.parquet');
-- expect: 0
SELECT count(never_set) FROM read_parquet('{dir}/nullable.parquet');
-- expect: 333667
SELECT sum(a) FROM read_parquet('{dir}/nullable.parquet');
-- expect: 66
SELECT count(*) FROM read_parquet('{dir}/nullable.parquet') WHERE a IS NULL AND s IS NULL;
-- expect: missing
SELECT coalesce(s, 'missing') FROM read_parquet('{dir}/nullable.parquet') WHERE id = 10;

-- nested.parquet: id 1..100
-- expect: 90
SELECT count(l) FROM read_parquet('{dir}/nested.parquet');
-- expect: 10
SELECT count(*) FROM read_parquet('{dir}/nested.parquet') WHERE len(l) = 0;
-- expect: 240
SELECT sum(len(l)) FROM read_parquet('{dir}/nested.parquet');
-- expect: 160
SELECT count(x) FROM (SELECT unnest(l) AS x FROM read_parquet('{dir}/nested.parquet'));
-- expect: 12000
SELECT sum(x) FROM (SELECT unnest(l) AS x FROM read_parquet('{dir}/nested.parquet'));
-- expect: [3, NULL, 6]
SELECT l FROM read_parquet('{dir}/nested.parquet') WHERE id = 3;
-- expect: 86
SELECT count(st) FROM read_parquet('{dir}/nested.parquet');
-- expect: n3
SELECT st.name FROM read_parquet('{dir}/nested.parquet') WHERE id = 3;
-- expect: t1,all
SELECT array_to_string(st.tags, ',') FROM read_parquet('{dir}/nested.parquet') WHERE id = 4;
-- expect: true
SELECT st IS NULL FROM read_parquet('{dir}/nested.parquet') WHERE id = 7;
-- expect: 5050
SELECT sum(m['k']) FROM read_parquet('{dir}/nested.parquet');
-- expect: true
SELECT m['none'] IS NULL AND cardinality(m) = 2 FROM read_parquet('{dir}/nested.parquet') WHERE id = 42;

-- multi_row_group.parquet: id 0..49999, bucket = id % 100
-- expect: 5
SELECT count(DISTINCT row_group_id) FROM parquet_metadata('{dir}/multi_row_group.parquet');
-- expect: 50000
SELECT count(*) FROM read_parquet('{dir}/multi_row_group.parquet');
-- expect: 1249975000
SELECT sum(id) FROM read_parquet('{dir}/multi_row_group.parquet');
-- expect: 2475000
SELECT sum(bucket) FROM read_parquet('{dir}/multi_row_group.parquet');
-- expect: 10
SELECT count(*) FROM read_parquet('{dir}/multi_row_group.parquet') WHERE id BETWEEN 30000 AND 30009;
-- expect: 500
SELECT count(*) FROM read_parquet('{dir}/multi_row_group.parquet') WHERE bucket = 7;
-- expect: row 10240
SELECT payload FROM read_parquet('{dir}/multi_row_group.parquet') WHERE id = 10240;
-- expect: row 49999
SELECT payload FROM read_parquet('{dir}/multi_row_group.parquet') WHERE id = 49999;
