"""Custom colormaps: committed files match the generator and the API serves them."""

import importlib.util
import pathlib

import numpy as np
import pytest
from test_app import store_params

REPO = pathlib.Path(__file__).resolve().parents[1]
CMAP_DIR = REPO / "src" / "titiler" / "multidim" / "cmap_data"

_spec = importlib.util.spec_from_file_location(
    "make_cmaps", REPO / "scripts" / "make_cmaps.py"
)
make_cmaps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_cmaps)


@pytest.mark.parametrize("name", list(make_cmaps.CMAPS))
def test_committed_file_matches_generator(name):
    committed = np.load(CMAP_DIR / f"{name}.npy")
    assert committed.shape == (256, 4) and committed.dtype == np.uint8
    np.testing.assert_array_equal(committed, make_cmaps.CMAPS[name]())


def test_rainbow_4_is_verbatim_worldview():
    lut = np.load(CMAP_DIR / "worldview_rainbow_4.npy")
    assert lut[0].tolist() == [0x3B, 0x76, 0xC6, 255]  # first Worldview color
    assert (
        lut[254].tolist() == lut[255].tolist() == [0x07, 0x04, 0x0D, 255]
    )  # last, repeated


def test_pm25_is_the_ozone_ramp_stretched_over_aqi_categories():
    o3 = np.load(CMAP_DIR / "naqfc_o3.npy")
    pm25 = np.load(CMAP_DIR / "naqfc_pm25.npy")
    assert pm25[0].tolist() == o3[0].tolist()  # 0 ug/m3 and 0 ppb share a color
    assert pm25[255].tolist() == o3[255].tolist()  # 225.4 ug/m3 and 200 ppb too
    # "Good" PM2.5 ends at 9 ug/m3 (index 10); "Good" ozone ends at 54 ppb (index 69).
    # Both are still green, and PM2.5 reaches it far earlier in the table.
    assert pm25[10][1] > pm25[10][0] and o3[69][1] > o3[69][0]
    assert not np.array_equal(pm25, o3)


def test_api_lists_and_serves_custom_colormaps(app):
    listed = {c["id"] for c in app.get("/colorMaps").json()["colormaps"]}
    for name in make_cmaps.CMAPS:
        assert name in listed
        assert app.get(f"/colorMaps/{name}").status_code == 200


def test_tile_renders_with_custom_colormap(app):
    params = {
        **store_params["zarr_store_v2"]["params"],
        "rescale": "0,1",
        "colormap_name": "naqfc_o3",
    }
    response = app.get("/tiles/WebMercatorQuad/0/0/0.png", params=params)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
