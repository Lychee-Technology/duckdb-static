-- Writes the v1 fixture files. test/fixtures/fixtures.py runs this with the
-- pinned DuckDB CLI, single-threaded, in the output directory. Every value is
-- computed from the row number, so the expected results in v1.queries.sql can
-- be worked out by hand.

-- 100 rows of common scalar types.
COPY (
	SELECT
		i::INTEGER AS id,
		'item ' || i AS name,
		(i * 1.25)::DECIMAL(10, 2) AS price,
		i % 2 = 0 AS even,
		DATE '2024-01-01' + i::INTEGER AS day
	FROM range(1, 101) t(i)
) TO 'basic.parquet' (FORMAT parquet, COMPRESSION snappy);

-- 1000 rows with NULLs: a is NULL when id is a multiple of 3, s when it is a
-- multiple of 5, and never_set always.
COPY (
	SELECT
		i::INTEGER AS id,
		CASE WHEN i % 3 <> 0 THEN i::INTEGER END AS a,
		CASE WHEN i % 5 <> 0 THEN 's' || i END AS s,
		NULL::INTEGER AS never_set
	FROM range(1, 1001) t(i)
) TO 'nullable.parquet' (FORMAT parquet, COMPRESSION snappy);

-- 100 rows of nested types. l is NULL when id ends in 0, empty when it ends in
-- 5, and [id, NULL, 2 * id] otherwise. st is NULL when id is a multiple of 7.
-- m always has the key 'k' and a key 'none' whose value is NULL.
COPY (
	SELECT
		i::INTEGER AS id,
		CASE
			WHEN i % 10 = 0 THEN NULL
			WHEN i % 10 = 5 THEN []::INTEGER[]
			ELSE [i::INTEGER, NULL, (2 * i)::INTEGER]
		END AS l,
		CASE WHEN i % 7 <> 0 THEN {'name': 'n' || i, 'tags': ['t' || (i % 3), 'all']} END AS st,
		MAP {'k': i::INTEGER, 'none': NULL::INTEGER} AS m
	FROM range(1, 101) t(i)
) TO 'nested.parquet' (FORMAT parquet, COMPRESSION snappy);

-- 50000 rows sorted by id in several row groups, so a predicate on id can skip
-- row groups and a projection reads only some column chunks. DuckDB rounds
-- ROW_GROUP_SIZE up to whole vectors of 2048 rows: four groups of 10240 rows
-- and one of 9040.
COPY (
	SELECT
		i AS id,
		(i % 100)::INTEGER AS bucket,
		'row ' || i AS payload
	FROM range(50000) t(i)
) TO 'multi_row_group.parquet' (FORMAT parquet, COMPRESSION snappy, ROW_GROUP_SIZE 10000);
