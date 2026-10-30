"""Custom colormaps: the tables keep their stated properties and the API serves them."""

import re
from itertools import pairwise
from urllib.parse import urlparse

import pytest
from test_app import store_params

from titiler.multidim import colormaps


@pytest.mark.parametrize("spec", colormaps.SPECS.values(), ids=colormaps.SPECS.keys())
def test_file_follows_schema(spec):
    assert isinstance(spec["name"], str)
    assert isinstance(spec["source"], str)
    assert urlparse(spec["reference"]).scheme == "https"
    assert isinstance(spec.get("notes", ""), str)
    keys = set(spec) - {"name", "source", "reference", "notes"}
    if "stops" in spec:
        assert keys == {"stops"}
        assert all(re.fullmatch(r"#[0-9A-Fa-f]{6}", c) for c in spec["stops"].values())
    else:
        assert keys == {"ramp", "breakpoints"}
        assert "stops" in colormaps.SPECS[spec["ramp"]]


def test_rainbow_4_is_verbatim_worldview():
    stops = colormaps.SPECS["worldview_rainbow_4"]["stops"]
    rainbow = colormaps.cmap.get("worldview_rainbow_4")

    # After rescale, pixel values run from 0 to 255, and rio-tiler leaves any
    # value without a row transparent, so the colormap needs all 256 rows.
    # A 256-row table, not the list of value ranges rio-tiler also accepts.
    assert isinstance(rainbow, dict)
    assert len(rainbow) == 256
    # One stop per row, so every row is a stop's color, unblended.
    assert list(rainbow.values()) == [
        (*bytes.fromhex(c[1:]), 255) for c in stops.values()
    ]
    # Worldview's first and last colors, the last repeated to fill row 255.
    assert rainbow[0] == (0x3B, 0x76, 0xC6, 255)
    assert rainbow[254] == rainbow[255] == (0x07, 0x04, 0x0D, 255)


def _interpolation_points():
    for name, spec in colormaps.SPECS.items():
        for key in ("stops", "breakpoints"):
            if key in spec:
                yield pytest.param([float(k) for k in spec[key]], id=f"{name} {key}")
        if "breakpoints" in spec:
            yield pytest.param(
                list(spec["breakpoints"].values()), id=f"{name} breakpoint values"
            )


@pytest.mark.parametrize("points", list(_interpolation_points()))
def test_interpolation_points_increase(points):
    # np.interp does not check that the points it interpolates between increase,
    # so an out-of-order entry silently scrambles a colormap rather than failing.
    out_of_order = [
        f"{a} is not less than the next point, {b}"
        for a, b in pairwise(points)
        if a >= b
    ]

    assert out_of_order == []


@pytest.mark.parametrize(
    "name",
    [
        "worldview_rainbow_4",
        "AQI O3 8hr breakpoints gradient",
        "AQI PM2.5 24hr breakpoints gradient",
    ],
)
def test_api_lists_and_serves_custom_colormap(app, name):
    listed = {c["id"] for c in app.get("/colorMaps").json()["colormaps"]}

    assert name in listed
    assert app.get(f"/colorMaps/{name}").status_code == 200


def test_tile_renders_with_custom_colormap(app):
    params = {
        **store_params["zarr_store_v2"]["params"],
        "rescale": "0,1",
        "colormap_name": "AQI O3 8hr breakpoints gradient",
    }
    response = app.get("/tiles/WebMercatorQuad/0/0/0.png", params=params)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
