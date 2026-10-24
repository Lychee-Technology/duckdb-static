-- The exact results the v2 fixtures must give, in the format of v1.queries.sql.

-- tiny.parquet: id 1..1200, bucket = id % 7
-- expect: 1200
SELECT count(*) FROM read_parquet('{dir}/tiny.parquet');
-- expect: 720600
SELECT sum(id) FROM read_parquet('{dir}/tiny.parquet');
-- expect: 3597
SELECT sum(bucket) FROM read_parquet('{dir}/tiny.parquet');
-- expect: 1199
SELECT count(*) FROM read_parquet('{dir}/tiny.parquet') WHERE id > 1;
