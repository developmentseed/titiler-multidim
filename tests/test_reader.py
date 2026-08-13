"""Reader tests."""

import numpy as np
import xarray as xr
from fakeredis import FakeRedis


def test_reader_reuses_cached_dataset(monkeypatch):
    """Reuse a cached dataset instead of reopening it in the parent reader."""
    import titiler.multidim.reader as reader

    opened = 0

    def opener(*args, **kwargs):
        nonlocal opened
        opened += 1
        return xr.Dataset(
            {"data": (("y", "x"), np.ones((2, 2)))},
            coords={"x": [0, 1], "y": [1, 0]},
        ).rio.write_crs("EPSG:4326")

    monkeypatch.setattr(reader, "cache_client", FakeRedis())
    monkeypatch.setattr(reader.api_settings, "enable_cache", True)
    monkeypatch.setattr(reader, "guess_opener", opener)

    reader.XarrayReader("cache.zarr", "data")
    reader.XarrayReader("cache.zarr", "data")

    assert opened == 1
