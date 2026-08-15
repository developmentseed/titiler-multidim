"""Xarray mosaic behavior."""

import numpy as np
import pytest
import xarray as xr
from obstore.exceptions import GenericError
from rio_tiler.mosaic.methods.defaults import FirstMethod, HighestMethod
from titiler.core.errors import BadRequestError

from titiler.multidim.mosaic import XarrayMosaicBackend


@pytest.fixture
def reader_cls(monkeypatch):
    """Return the current reader class with its Redis cache disabled."""
    from titiler.multidim import reader

    monkeypatch.setattr(reader.api_settings, "enable_cache", False)
    return reader.XarrayReader


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
