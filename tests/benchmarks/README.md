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

## Reporting a PR's performance

Same machine, same interpreter, nothing else running. Build `main` in a
worktree so the branch stays checked out:

```sh
git worktree add ../titiler-multidim-main main
(cd ../titiler-multidim-main && uv run pytest tests/benchmarks --benchmark-only --no-cov -q --benchmark-json=../before.json)
uv run pytest tests/benchmarks --benchmark-only --no-cov -q --benchmark-json=../after.json
uv run pytest-benchmark compare ../before.json ../after.json --columns=median,min,rounds --sort=name
```

Paste that table into the PR's **Performance** section, together with the
diff of `tests/test_perf_counts.py` if the PR changed any pinned count
(opens, storage requests or boto3 sessions per request). Medians under
~20 ms move by a few percent between runs; call a change real only when it
is well outside that. Benchmarks need no `-n`: pytest-benchmark turns
itself off under xdist.
