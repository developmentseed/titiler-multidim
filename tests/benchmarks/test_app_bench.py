"""App-level benchmarks on a 17,228-step Icechunk store (TEMPO's time axis length).

Each case is one request through `TestClient`, so it includes dependency
parsing, the mosaic backend, the reader and rendering. Compare runs with
`pytest-benchmark compare` (see README).
"""

import pytest

TIME = "2023-08-01T00:00:00"


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/info", {"sel": f"time={TIME}"}),
        # rio-tiler info() builds band metadata for every time step: Issue 4
        ("/info", {}),
        # plus the times list (cheap since #194; kept so a regression shows)
        ("/info", {"show_times": True}),
        ("/tiles/WebMercatorQuad/2/0/1.png", {"sel": f"time={TIME}"}),
        ("/point/-95,35", {"sel": f"time={TIME}"}),
    ],
    ids=["info_sel", "info_no_sel", "info_show_times", "tile_z2", "point"],
)
def test_request(benchmark, app, long_icechunk_store, path, params):
    """One request, cold: every round opens the store again (as on main).

    The data chunks are inlined in the manifest (see the fixture), so the
    tile and point cases measure the open and request path, not chunk
    fetches; the chunk counts live in tests/test_perf_counts.py.
    """
    query = {"url": long_icechunk_store, "variable": "data", **params}

    def run():
        response = app.get(path, params=query)
        assert response.status_code == 200

    benchmark(run)
