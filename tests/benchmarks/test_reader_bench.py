"""XarrayReader benchmarks: open, tile and point.

Cases are parametrized by store latency (0 and 30 ms per `get`) so the
difference between "reads the window" and "reads the whole slice", and
between one batched round trip and several, shows up as time and not
just as a `get` count. Values compared across branches must come from
the same machine, interpreter and store layout.
"""

import numpy as np
import pytest
import xarray as xr
from zarr.abc.store import Store
from zarr.storage import LocalStore, WrapperStore
from zarr.testing.store import LatencyStore

from titiler.multidim import mosaic, reader

LATENCIES = (0.0, 0.03)
# WebMercatorQuad z/x/y inside the fixture's bounds
TILE = (3, 2, 3)
POINT = (-95.0, 35.0)


class CountingStore(WrapperStore[Store]):
    """Count `get` calls on the wrapped store.

    Wrap a read-only store: zarr then uses this instance as-is rather than
    deriving a read-only copy, with its own count, through `_with_store`.
    """

    gets = 0

    async def get(self, key, prototype, byte_range=None):
        """Count, then delegate."""
        self.gets += 1
        return await self._store.get(key, prototype, byte_range=byte_range)

    def get_sync(self, key, prototype, byte_range=None):
        """Count, then delegate (zarr >= 3.2 sync read path)."""
        self.gets += 1
        return self._store.get_sync(key, prototype=prototype, byte_range=byte_range)


@pytest.fixture(scope="session")
def store_path(tmp_path_factory) -> str:
    """4 x 720 x 1440 float32 data, 2-D mask and flag, chunked 1 x 180 x 180."""
    rng = np.random.default_rng(0)
    ny, nx, nt = 720, 1440, 4
    flag = rng.integers(0, 3, (ny, nx)).astype("float32")
    flag[::7, ::11] = np.nan  # fill pixels
    ds = xr.Dataset(
        {
            "data": (("time", "lat", "lon"), rng.random((nt, ny, nx), dtype="float32")),
            "mask2d": (("lat", "lon"), rng.random((ny, nx), dtype="float32")),
            "flag": (("lat", "lon"), flag),
        },
        coords={
            "time": np.arange(nt),
            "lat": np.linspace(-89.875, 89.875, ny),
            "lon": np.linspace(-179.875, 179.875, nx),
        },
    )
    path = str(tmp_path_factory.mktemp("bench") / "store.zarr")
    ds.to_zarr(
        path,
        consolidated=False,
        encoding={
            "data": {"chunks": (1, 180, 180)},
            "mask2d": {"chunks": (180, 180)},
            "flag": {"chunks": (180, 180)},
        },
    )
    return path


@pytest.fixture(params=LATENCIES, ids=lambda v: f"latency={int(v * 1000)}ms")
def latency(request) -> float:
    """Per-`get` store latency in seconds."""
    return request.param


@pytest.fixture
def counter(store_path, latency, monkeypatch) -> CountingStore:
    """Route the reader's opener through counting + latency store wrappers."""
    counting = CountingStore(LocalStore(store_path, read_only=True))
    store: Store = LatencyStore(counting, get_latency=latency) if latency else counting

    def opener(src_path: str, **kwargs) -> xr.Dataset:
        return xr.open_zarr(store, chunks=None, consolidated=False, decode_coords="all")

    monkeypatch.setattr(reader, "guess_opener", opener)
    return counting


def _reader():
    return reader.XarrayReader(
        src_path="bench://store", variable="data", sel=["time=0"]
    )


def _record(benchmark, counter, fn):
    """Time `fn`; record the `get` count of one call as extra_info."""
    counter.gets = 0
    fn()
    assert counter.gets, "gets went to a copy of the counting store"
    benchmark.extra_info["store_gets"] = counter.gets
    benchmark(fn)


def test_open(benchmark, counter):
    """Reader construction: open and extract the variable (metadata only)."""

    def run():
        with _reader():
            pass

    _record(benchmark, counter, run)


def test_tile(benchmark, counter):
    """Open + one z3 tile: the request path a map viewer exercises."""

    def run():
        with _reader() as src:
            return src.tile(TILE[1], TILE[2], TILE[0])

    _record(benchmark, counter, run)


def test_point(benchmark, counter):
    """Open + one point read."""

    def run():
        with _reader() as src:
            return src.point(*POINT)

    _record(benchmark, counter, run)


def _backend():
    """Single-URL request path: the backend validates, then rio-tiler reads."""
    return mosaic.XarrayMosaicBackend(
        ["bench://store"],
        reader=reader.XarrayReader,
        reader_options={"variable": "data", "sel": ["time=0"]},
    )


def test_backend_tile(benchmark, counter):
    """Open through XarrayMosaicBackend + one z3 tile, as /tiles does."""

    def run():
        with _backend() as src:
            return src.tile(TILE[1], TILE[2], TILE[0])

    _record(benchmark, counter, run)


def test_backend_point(benchmark, counter):
    """Open through XarrayMosaicBackend + one point read, as /point does."""

    def run():
        with _backend() as src:
            return src.point(*POINT)

    _record(benchmark, counter, run)
