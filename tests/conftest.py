"""Shared test fixtures."""

import json
import sys
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app(monkeypatch):
    """Return a test client for the application."""
    # Set environment variables using monkeypatch (auto-cleanup)
    monkeypatch.setenv("TITILER_MULTIDIM_DEBUG", "TRUE")
    # virtual container auth for icechunk tests
    monkeypatch.setenv(
        "TITILER_MULTIDIM_AUTHORIZED_CHUNK_ACCESS",
        json.dumps(
            {"s3://nasa-waterinsight/NLDAS3/forcing/daily/": {"anonymous": True}}
        ),
    )

    # Clear module cache to ensure fresh import
    modules_to_clear = [
        key for key in sys.modules.keys() if key.startswith("titiler.multidim")
    ]
    for module in modules_to_clear:
        del sys.modules[module]

    from titiler.multidim.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="session")
def long_icechunk_store(tmp_path_factory) -> str:
    """Icechunk store with TEMPO's time axis length (17,228 steps) on an 8x8 grid.

    Data is chunked coarsely: one chunk per step takes ~8 s to write and 17k
    files, and nothing here depends on it. The `time` coordinate is the only
    chunk object in the store (~18 KB stored): the all-zero data chunks
    compress below icechunk's inline threshold and live in the manifest, so
    tile and point requests on this store never fetch a data chunk.
    """
    import icechunk
    import numpy as np
    import xarray as xr

    nt, ny, nx = 17228, 8, 8
    ds = xr.Dataset(
        {
            "data": (
                ("time", "latitude", "longitude"),
                np.zeros((nt, ny, nx), "float32"),
            )
        },
        coords={
            "time": np.datetime64("2023-08-01T00:00:00", "ns")
            + np.arange(nt) * np.timedelta64(40, "m"),
            "latitude": np.linspace(50, 15, ny),
            "longitude": np.linspace(-125, -65, nx),
        },
    )
    path = str(tmp_path_factory.mktemp("icechunk") / "long")
    repo = icechunk.Repository.create(icechunk.local_filesystem_storage(path))
    session = repo.writable_session("main")
    ds.to_zarr(
        session.store, consolidated=False, encoding={"data": {"chunks": (1000, ny, nx)}}
    )
    session.commit("17,228 time steps")
    return path
