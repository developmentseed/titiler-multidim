# Reader benchmarks

In-process `XarrayReader` timings (pytest-benchmark) on a generated zarr
store, with the store optionally wrapped in zarr's `LatencyStore` so each
`get` costs a fixed round trip, as it would from S3. Every case also
records the number of store `get` calls one operation makes
(`extra_info.store_gets`), which is exact and branch-comparable.

```sh
uv run pytest tests/benchmarks --benchmark-only --no-cov -q \
    --benchmark-json=bench.json --benchmark-columns=median,min,rounds
```

Compare two runs (e.g. two branches):

```sh
uv run pytest-benchmark compare a.json b.json --columns=median --sort=name
```
