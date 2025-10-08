import json
import os
import pytest

from helpers import find_string_in_stream


DATA_DIR = "tests/fixtures"
test_zarr_store_v2 = os.path.join(DATA_DIR, "zarr_store_v2.zarr")
test_zarr_store_v3 = os.path.join(DATA_DIR, "zarr_store_v3.zarr")
test_netcdf_store = os.path.join(DATA_DIR, "testfile.nc")
test_unconsolidated_store = os.path.join(DATA_DIR, "unconsolidated.zarr")
test_pyramid_store = os.path.join(DATA_DIR, "pyramid.zarr")
test_icechunk_native = os.path.join(DATA_DIR, "icechunk_native")

store_params = {}

store_params["zarr_store_v2"] = {
    "params": {
        "url": test_zarr_store_v2,
        "variable": "CDD0",
        "decode_times": False,
        "sel": "time=0",
    },
    "variables": ["CDD0", "DISPH", "FROST_DAYS", "GWETPROF"],
}

store_params["zarr_store_v3"] = {
    "params": {
        "url": test_zarr_store_v3,
        "variable": "CDD0",
        "decode_times": False,
        "sel": "time=0",
    },
    "variables": ["CDD0", "DISPH", "FROST_DAYS", "GWETPROF"],
}

store_params["icechunk_native"] = {
    "params": {
        "url": test_icechunk_native,
        "variable": "CDD0",
        "decode_times": False,
        "sel": "time=0",
    },
    "variables": ["CDD0", "DISPH", "FROST_DAYS", "GWETPROF"],
}

store_params["netcdf_store"] = {
    "params": {
        "url": test_netcdf_store,
        "variable": "data",
        "decode_times": False,
        "sel": "time=0",
    },
    "variables": ["data"],
}
store_params["unconsolidated_store"] = {
    "params": {
        "url": test_unconsolidated_store,
        "variable": "var1",
        "decode_times": False,
        "sel": "time=0",
    },
    "variables": ["var1", "var2"],
}
store_params["pyramid_store"] = {
    "params": {
        "url": test_pyramid_store,
        "variable": "value",
        "decode_times": False,
        "group": "2",
        "sel": "time=0",
    },
    "variables": ["value"],
}


def get_variables_test(app, ds_params):
    response = app.get("/variables", params=ds_params["params"])
    assert response.status_code == 200
    # TODO: Do we care about the order?
    assert set(response.json()) == set(ds_params["variables"])
    assert response.headers["server-timing"]
    timings = response.headers["server-timing"].split(",")
    assert len(timings) == 2
    assert timings[0].startswith("total;dur=")
    assert timings[1].lstrip().startswith("1-xarray-open_dataset;dur=")


@pytest.mark.parametrize("store_params", store_params.values(), ids=store_params.keys())
def test_get_variables(store_params, app):
    return get_variables_test(app, store_params)


def get_info_test(app, ds_params):
    response = app.get(
        "/info",
        params=ds_params["params"],
    )
    assert response.status_code == 200
    expectation_fn = f"{ds_params['params']['url'].replace(DATA_DIR, f'{DATA_DIR}/responses').replace('.', '_')}_info.json"
    with open(
        expectation_fn,
        "r",
    ) as f:
        assert response.json() == json.load(f)


@pytest.mark.parametrize("store_params", store_params.values(), ids=store_params.keys())
def test_get_info(store_params, app):
    return get_info_test(app, store_params)


def get_tilejson_test(app, ds_params):
    response = app.get(
        "/WebMercatorQuad/tilejson.json",
        params=ds_params["params"],
    )
    assert response.status_code == 200
    expectation_fn = f"{ds_params['params']['url'].replace(DATA_DIR, f'{DATA_DIR}/responses').replace('.', '_')}_tilejson.json"

    with open(
        expectation_fn,
        "r",
    ) as f:
        assert response.json() == json.load(f)


@pytest.mark.parametrize("store_params", store_params.values(), ids=store_params.keys())
def test_get_tilejson(store_params, app):
    return get_tilejson_test(app, store_params)


def get_tile_test(app, ds_params, zoom: int = 0):
    response = app.get(
        f"/tiles/WebMercatorQuad/{zoom}/0/0.png",
        params=ds_params["params"],
    )
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "image/png"
    assert response.headers["server-timing"]
    timings = response.headers["server-timing"].split(",")
    assert len(timings) == 3
    assert timings[1].lstrip().startswith("1-xarray-open_dataset;dur=")
    assert timings[2].lstrip().startswith("2-rioxarray-reproject;dur=")


@pytest.mark.parametrize("store_params", store_params.values(), ids=store_params.keys())
def test_get_tile(store_params, app):
    # if the store is a pyramid we test zoom levels 0-2
    if "group" in store_params["params"]:
        for z in range(3):
            get_tile_test(app, store_params, zoom=z)
    else:
        get_tile_test(app, store_params)


def histogram_test(app, ds_params):
    response = app.get(
        "/histogram",
        params=ds_params["params"],
    )
    assert response.status_code == 200
    with open(
        f"{ds_params['params']['url'].replace(DATA_DIR, f'{DATA_DIR}/responses').replace('.', '_')}_histogram.json",
        "r",
    ) as f:
        assert response.json() == json.load(f)


@pytest.mark.parametrize("store_params", store_params.values(), ids=store_params.keys())
def test_histogram(store_params, app):
    return histogram_test(app, store_params)


# TODO: Maybe this is overkill to parametrize?
@pytest.mark.parametrize("store_params", store_params.values(), ids=store_params.keys())
def test_histogram_error(store_params, app):
    store_path = store_params["params"]["url"]
    response = app.get(
        "/histogram",
        params={"url": store_path},
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "type": "missing",
                "loc": ["query", "variable"],
                "msg": "Field required",
                "input": None,
            }
        ]
    }


def test_map_without_params(app):
    response = app.get("/WebMercatorQuad/map")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "text/html; charset=utf-8"
    assert find_string_in_stream(response, "Step 1: Enter the URL of your Zarr store")


@pytest.mark.parametrize("store_params", store_params.values(), ids=store_params.keys())
def test_map_with_params(store_params, app):
    store_path = store_params["params"]["url"]
    variable = store_params["variables"][0]
    response = app.get(
        "/WebMercatorQuad/map", params={"url": store_path, "variable": variable}
    )
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "text/html; charset=utf-8"
    assert find_string_in_stream(response, '<div id="map"></div>')
