"""Xarray mosaic behavior."""

import numpy as np
import pytest
import xarray as xr
from rasterio.crs import CRS
from obstore.exceptions import GenericError
from rio_tiler.mosaic.methods.defaults import FirstMethod, HighestMethod
from titiler.core.errors import BadRequestError

from titiler.multidim.mosaic import XarrayMosaicBackend


@pytest.fixture
def reader_cls():
    """Return the current reader class."""
    from titiler.multidim import reader

    return reader.XarrayReader


@pytest.fixture
def opens(app, monkeypatch):
    """Record every dataset open made through the reader's opener.

    Depends on ``app`` because that fixture re-imports ``titiler.multidim``,
    which would discard a patch made before it ran.
    """
    from titiler.multidim import reader

    calls = []
    original = reader.guess_opener

    def counting(src_path, **kwargs):
        calls.append(src_path)
        return original(src_path, **kwargs)

    monkeypatch.setattr(reader, "guess_opener", counting)
    return calls


def write_dataset(path, value, *, x=(-5.0, 5.0), times=None, extra_variable=False):
    """Write a small geographic NetCDF dataset with a known value."""
    times = times or [0]
    data = np.full((len(times), 2, 2), value, dtype="float32")
    dataset = xr.Dataset(
        {"data": (("time", "lat", "lon"), data)},
        coords={"time": times, "lat": [-5.0, 5.0], "lon": list(x)},
    ).rio.write_crs("EPSG:4326")
    if extra_variable:
        dataset["other"] = dataset["data"]
    dataset.to_netcdf(path, engine="h5netcdf")


def write_antimeridian_dataset(path):
    """Write a polar stereographic dataset that crosses the antimeridian."""
    dataset = xr.Dataset(
        {"data": (("y", "x"), np.ones((553, 825), dtype="float32"))},
        coords={
            "x": np.linspace(-2622372.468724773, 2288852.531275227, 825),
            "y": np.linspace(-4813079.750943905, -1521070.7509439047, 553),
        },
    ).rio.write_crs(
        CRS.from_proj4(
            "+proj=stere +lat_0=90 +lat_ts=60 +lon_0=210 "
            "+a=6371229 +b=6371229 +units=m +no_defs"
        )
    )
    dataset.to_netcdf(path, engine="h5netcdf")


def write_coordinate_labeled_dataset(path, label):
    """Write a geographic dataset with an auxiliary display coordinate."""
    dataset = xr.Dataset(
        {"data": (("y", "x"), np.ones((2, 2), dtype="float32"))},
        coords={
            "x": [-5.0, 5.0],
            "y": [-5.0, 5.0],
            "label": (("y", "x"), np.full((2, 2), label)),
        },
    ).rio.write_crs("EPSG:4326")
    dataset.to_netcdf(path, engine="h5netcdf")


@pytest.fixture
def sources(tmp_path):
    """Create compatible adjacent and overlapping Xarray sources."""
    left = tmp_path / "left.nc"
    right = tmp_path / "right.nc"
    first = tmp_path / "first.nc"
    second = tmp_path / "second.nc"
    write_dataset(left, 1, x=(-7.5, -2.5))
    write_dataset(right, 2, x=(2.5, 7.5))
    write_dataset(first, 1)
    write_dataset(second, 2)
    return left, right, first, second


def test_backend_filters_assets_in_request_order(sources, reader_cls):
    """Adjacent sources are filtered spatially without changing their priority."""
    left, right, _, _ = sources
    backend = XarrayMosaicBackend(
        [str(left), str(right)],
        reader=reader_cls,
        reader_options={"variable": "data"},
    )

    assert backend.assets_for_bbox(-6, -1, 6, 1) == [str(left), str(right)]
    assert backend.assets_for_point(5, 0) == [str(right)]


def test_backend_selects_antimeridian_crossing_assets(tmp_path, reader_cls):
    """Antimeridian-crossing source bounds include points on both sides."""
    source = tmp_path / "antimeridian.nc"
    write_antimeridian_dataset(source)
    backend = XarrayMosaicBackend(
        [str(source)], reader=reader_cls, reader_options={"variable": "data"}
    )

    assert backend.assets_for_point(170, 60) == [str(source)]
    assert backend.assets_for_point(-150, 60) == [str(source)]


def test_backend_reports_antimeridian_crossing_mosaic_bounds(sources, reader_cls):
    """Adjacent sources across the antimeridian retain wrapped bounds."""
    west, east, _, _ = sources
    write_dataset(west, 1, x=(172.5, 177.5))
    write_dataset(east, 2, x=(-177.5, -172.5))

    backend = XarrayMosaicBackend(
        [str(west), str(east)], reader=reader_cls, reader_options={"variable": "data"}
    )

    assert backend.bounds == (170.0, -10.0, -170.0, 10.0)


def test_backend_ignores_auxiliary_coordinate_labels(tmp_path, reader_cls):
    """Auxiliary coordinates do not make compatible sources incompatible."""
    first = tmp_path / "first.nc"
    second = tmp_path / "second.nc"
    write_coordinate_labeled_dataset(first, 1)
    write_coordinate_labeled_dataset(second, 2)

    backend = XarrayMosaicBackend(
        [str(first), str(second)],
        reader=reader_cls,
        reader_options={"variable": "data"},
    )

    assert backend.assets_for_point(0, 0) == [str(first), str(second)]


def test_backend_composes_points_with_requested_strategy(sources, reader_cls):
    """Point composition uses rio-tiler's first and highest methods."""
    _, _, first, second = sources
    backend = XarrayMosaicBackend(
        [str(first), str(second)],
        reader=reader_cls,
        reader_options={"variable": "data"},
    )

    point, _ = backend.point(0, 0, pixel_selection=FirstMethod)
    assert point.array.tolist() == [1.0]

    point, _ = backend.point(0, 0, pixel_selection=HighestMethod)
    assert point.array.tolist() == [2.0]

    image, _ = backend.part(
        (-10, -10, 10, 10), width=2, height=2, pixel_selection=HighestMethod
    )
    assert image.array.compressed().tolist() == [2.0] * 4


def test_backend_rejects_unreadable_and_incompatible_sources(
    sources, tmp_path, reader_cls
):
    """Construction validates every requested source before it can be mosaicked."""
    left, _, _, _ = sources
    incompatible = tmp_path / "incompatible.nc"
    write_dataset(incompatible, 2, times=[0, 1])

    with pytest.raises(GenericError):
        XarrayMosaicBackend(
            [str(left), str(tmp_path / "missing.nc")],
            reader=reader_cls,
            reader_options={"variable": "data"},
        )

    with pytest.raises(BadRequestError):
        XarrayMosaicBackend(
            [str(left), str(incompatible)],
            reader=reader_cls,
            reader_options={"variable": "data"},
        )


def test_mosaic_endpoints_preserve_shapes_and_compose_data(app, sources):
    """Multiple URLs retain Xarray responses while reporting aggregate coverage."""
    left, right, first, second = sources
    adjacent = [("url", str(left)), ("url", str(right)), ("variable", "data")]

    info = app.get("/info", params=adjacent)
    assert info.status_code == 200
    assert info.json()["bounds"] == [-10.0, -10.0, 10.0, 10.0]
    assert "width" not in info.json()
    assert "height" not in info.json()

    point = app.get(
        "/point/0,0",
        params=[("url", str(first)), ("url", str(second)), ("variable", "data")],
    )
    assert point.status_code == 200
    assert point.json()["values"] == [1.0]

    highest = app.get(
        "/point/0,0",
        params=[
            ("url", str(first)),
            ("url", str(second)),
            ("variable", "data"),
            ("pixel_selection", "highest"),
        ],
    )
    assert highest.status_code == 200
    assert highest.json()["values"] == [2.0]

    histogram = app.get(
        "/histogram",
        params=[
            ("url", str(first)),
            ("url", str(second)),
            ("variable", "data"),
            ("pixel_selection", "highest"),
        ],
    )
    assert histogram.status_code == 200
    assert histogram.json()[0]["bucket"][0] == 1.5


def test_tile_outside_mosaic_returns_no_content(app, sources):
    """A tile outside all requested sources returns no content."""
    left, right, _, _ = sources
    response = app.get(
        "/tiles/WebMercatorQuad/2/0/0.png",
        params=[("url", str(left)), ("url", str(right)), ("variable", "data")],
    )

    assert response.status_code == 204


def test_mosaic_url_limits_and_variable_mismatch(app, sources, tmp_path):
    """The API validates URL cardinality and common variable namespaces."""
    left, _, _, _ = sources
    mismatched = tmp_path / "mismatched.nc"
    write_dataset(mismatched, 2, extra_variable=True)

    assert app.get("/info", params={"variable": "data"}).status_code == 422
    assert (
        app.get(
            "/info",
            params=[("url", str(left))] * 21 + [("variable", "data")],
        ).status_code
        == 422
    )
    assert (
        app.get("/variables", params=[("url", str(left)), ("url", str(mismatched))])
    ).status_code == 400


def test_histogram_supports_antimeridian_sources(app, tmp_path):
    """Histogram pools values from both sides of the antimeridian."""
    west = tmp_path / "am-west.nc"
    east = tmp_path / "am-east.nc"
    write_dataset(west, 1, x=(172.5, 177.5))
    write_dataset(east, 2, x=(-177.5, -172.5))

    response = app.get(
        "/histogram",
        params=[("url", str(west)), ("url", str(east)), ("variable", "data")],
    )
    assert response.status_code == 200
    histogram = response.json()
    assert sum(bucket["value"] for bucket in histogram) > 0
    assert histogram[0]["bucket"][0] == 1.0
    assert histogram[-1]["bucket"][1] == 2.0

    projected = tmp_path / "am-projected.nc"
    write_antimeridian_dataset(projected)
    response = app.get(
        "/histogram", params=[("url", str(projected)), ("variable", "data")]
    )
    assert response.status_code == 200
    histogram = response.json()
    assert sum(bucket["value"] for bucket in histogram) > 0
    assert histogram[0]["bucket"][0] == 0.5
    assert histogram[-1]["bucket"][1] == 1.5


@pytest.mark.parametrize(
    "route",
    [
        "/tiles/WebMercatorQuad/0/0/0.png",
        "/tiles/WGS1984Quad/0/0/0.png",
        "/point/0,0",
        "/bbox/-10,-10,10,10.png",
    ],
)
def test_single_source_routes_open_dataset_once(app, opens, sources, route):
    """A single-URL read opens its dataset once, not again after validation."""
    _, _, first, _ = sources
    response = app.get(route, params={"url": str(first), "variable": "data"})

    assert response.status_code == 200
    assert opens == [str(first)]


def test_mosaic_tile_opens_each_source_once(app, opens, sources):
    """A multi-URL tile opens every source exactly once."""
    _, _, first, second = sources
    response = app.get(
        "/tiles/WebMercatorQuad/0/0/0.png",
        params=[("url", str(first)), ("url", str(second)), ("variable", "data")],
    )

    assert response.status_code == 200
    assert sorted(opens) == sorted([str(first), str(second)])


def test_backend_opens_duplicate_urls_once(opens, sources, reader_cls):
    """Repeating a URL reuses its reader instead of opening it again."""
    _, _, first, _ = sources
    with XarrayMosaicBackend(
        [str(first), str(first)], reader=reader_cls, reader_options={"variable": "data"}
    ) as backend:
        point, _ = backend.point(0, 0)

    assert point.array.tolist() == [1.0]
    assert opens == [str(first)]


def _recording(reader_cls, closed):
    """Return a reader subclass that records which sources were closed."""

    class Recording(reader_cls):
        def close(self):
            closed.append(self.src_path)
            super().close()

    return Recording


def test_backend_closes_readers_on_exit(sources, reader_cls):
    """Readers stay open across reads and close when the backend exits."""
    _, _, first, second = sources
    closed = []
    with XarrayMosaicBackend(
        [str(first), str(second)],
        reader=_recording(reader_cls, closed),
        reader_options={"variable": "data"},
    ) as backend:
        backend.point(0, 0)
        backend.tile(0, 0, 0)
        assert closed == []

    assert sorted(closed) == sorted([str(first), str(second)])


def test_backend_closes_readers_when_validation_fails(sources, tmp_path, reader_cls):
    """Readers opened before a validation failure are closed, and it still raises."""
    left, _, _, _ = sources
    incompatible = tmp_path / "incompatible.nc"
    write_dataset(incompatible, 2, times=[0, 1])

    closed = []
    with pytest.raises(GenericError):
        XarrayMosaicBackend(
            [str(left), str(tmp_path / "missing.nc")],
            reader=_recording(reader_cls, closed),
            reader_options={"variable": "data"},
        )
    assert closed == [str(left)]

    closed.clear()
    with pytest.raises(BadRequestError):
        XarrayMosaicBackend(
            [str(left), str(incompatible)],
            reader=_recording(reader_cls, closed),
            reader_options={"variable": "data"},
        )
    assert sorted(closed) == sorted([str(left), str(incompatible)])


def test_info_show_times_opens_dataset_once(app, opens, sources):
    """Listing times reuses the reader opened for info."""
    _, _, first, _ = sources
    response = app.get(
        "/info", params={"url": str(first), "variable": "data", "show_times": "true"}
    )

    assert response.status_code == 200
    assert response.json()["times"] == ["0"]
    assert opens == [str(first)]
