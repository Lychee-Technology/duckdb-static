-- Writes the v2 fixture files, the same way as v1.sql (see there).

-- About 10 KB: one row group of 1200 rows. example/bench/ reads it over HTTP
-- as the benchmark's first query, so it is small enough that the query's time
-- isn't mostly transfer, and sum(id) has to read the id column chunk's pages,
-- which hold most of the file.
COPY (
	SELECT
		i AS id,
		(i % 7)::INTEGER AS bucket
	FROM range(1, 1201) t(i)
) TO 'tiny.parquet' (FORMAT parquet, COMPRESSION uncompressed);
