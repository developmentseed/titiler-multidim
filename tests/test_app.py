import json
import os
from urllib.parse import urlencode

import pytest
from helpers import find_string_in_stream

DATA_DIR = "tests/fixtures"
test_zarr_store_v2 = os.path.join(DATA_DIR, "zarr_store_v2.zarr")
test_zarr_store_v3 = os.path.join(DATA_DIR, "zarr_store_v3.zarr")
test_netcdf_store = os.path.join(DATA_DIR, "testfile.nc")
test_unconsolidated_store = os.path.join(DATA_DIR, "unconsolidated.zarr")
test_pyramid_store = os.path.join(DATA_DIR, "pyramid.zarr")
test_icechunk_native = os.path.join(DATA_DIR, "icechunk_native")
test_icechunk_virtual_accessible = os.path.join(DATA_DIR, "icechunk_virtual_accessible")

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

store_params["icechunk_virtual_accessible"] = {
    "params": {
        "url": test_icechunk_virtual_accessible,
        "variable": "LWdown",
        "decode_times": False,
        "sel": "time=1.0",
    },
    "variables": [
        "LWdown",
        "Wind_N",
        "Tair",
        "Rainf",
        "Wind_E",
        "Qair",
        "Tair_max",
        "Tair_min",
        "SWdown",
        "PSurf",
    ],
}

store_params["netcdf_store"] = {
    "params": {
        "url": test_netcdf_store,
        "variable": "data",
        "decode_times": True,
        "sel": "time=2020-01-01",
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
    response = app.get("/WebMercatorQuad/map.html")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "text/html; charset=utf-8"
    assert find_string_in_stream(response, "Step 1: Enter the URL of your Zarr store")


@pytest.mark.parametrize("store_params", store_params.values(), ids=store_params.keys())
def test_map_with_params(store_params, app):
    store_path = store_params["params"]["url"]
    variable = store_params["variables"][0]
    response = app.get(
        "/WebMercatorQuad/map.html", params={"url": store_path, "variable": variable}
    )
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "text/html; charset=utf-8"
    assert find_string_in_stream(response, '<div id="map"></div>')
    assert find_string_in_stream(response, "tilesize=256")
    point_query = urlencode({"url": store_path, "variable": variable})
    assert find_string_in_stream(
        response, f"point/{{lon}},{{lat}}?{point_query}`.replace"
    )


def test_legacy_map_redirect(app):
    params = {"url": test_zarr_store_v2, "variable": "CDD0"}
    response = app.get("/WebMercatorQuad/map", params=params, follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == (
        f"/WebMercatorQuad/map.html?{urlencode(params)}"
    )


def test_tilejson_forwards_tilesize(app):
    params = {**store_params["zarr_store_v2"]["params"], "tilesize": 256}
    response = app.get("/WebMercatorQuad/tilejson.json", params=params)

    assert response.status_code == 200
    assert "@" not in response.json()["tiles"][0]
    assert "tilesize=256" in response.json()["tiles"][0]


def test_sel_nearest_netcdf(app):
    params = store_params["netcdf_store"]["params"].copy()
    params["sel"] = "time=nearest::2020-01-06"
    response = app.get("/info", params=params)
    assert response.status_code == 200


def test_earthdata_exception_handlers_registered(app):
    from earthaccess_auth.exceptions import (
        LoginStrategyUnavailable,
        S3CredentialsRequestFailure,
    )

    from titiler.multidim.main import app as fastapi_app

    assert S3CredentialsRequestFailure in fastapi_app.exception_handlers
    assert LoginStrategyUnavailable in fastapi_app.exception_handlers


def test_earthdata_auth_failure_returns_403(app, monkeypatch):
    """A typed EULA rejection raised by the opener surfaces as HTTP 403.

    The unit tests in test_chunk_access.py verify the typed
    S3CredentialsRequestFailure escapes in Python (not wrapped by icechunk's
    Rust layer); this test verifies the app maps it to 403 with the EULA
    message intact.
    """
    from earthaccess_auth.exceptions import S3CredentialsRequestFailure

    def raise_eula(*args, **kwargs):
        raise S3CredentialsRequestFailure(
            "EULA not accepted: https://urs.earthdata.nasa.gov/approve"
        )

    monkeypatch.setattr("titiler.multidim.reader.guess_opener", raise_eula)
    response = app.get("/variables", params={"url": "s3://asdc-prod-protected/store"})
    assert response.status_code == 403
    assert "EULA" in response.text


def test_eula_403_does_not_echo_daac_response_body(app, monkeypatch):
    """earthaccess-auth embeds up to 1000 chars of the DAAC's raw HTTP
    response in S3CredentialsRequestFailure; that body (internal
    hostnames, correlation IDs, maintenance pages) must go to the service
    log only, never to unauthenticated callers. The client gets a fixed
    message pointing at the EDL EULA/application pages."""
    from earthaccess_auth.exceptions import S3CredentialsRequestFailure

    def raise_failure(*args, **kwargs):
        raise S3CredentialsRequestFailure(
            "The s3credentials endpoint https://internal.example/s3credentials "
            "rejected the request with status 500:\n"
            "DAAC-INTERNAL-BODY correlation-id=abc123\n"
            "Consider accepting the EULAs available at "
            "https://urs.earthdata.nasa.gov/users/earthaccess/unaccepted_eulas "
            "and applications at https://urs.earthdata.nasa.gov/application_search."
        )

    monkeypatch.setattr("titiler.multidim.reader.guess_opener", raise_failure)
    response = app.get("/variables", params={"url": "s3://asdc-prod-protected/store"})
    assert response.status_code == 403
    assert "DAAC-INTERNAL-BODY" not in response.text
    assert "internal.example" not in response.text
    assert "EULA" in response.text
    assert "urs.earthdata.nasa.gov" in response.text


def test_login_attempt_failure_returns_sanitized_500(app, monkeypatch):
    """LoginAttemptFailure carries the raw EDL response body (HTML error or
    maintenance pages); it must be mapped and sanitized, not fall through
    to the catch-all 500 that returns str(exc) verbatim."""
    from earthaccess_auth.exceptions import LoginAttemptFailure

    def raise_failure(*args, **kwargs):
        raise LoginAttemptFailure(
            "Authentication with Earthdata Login failed with:\n"
            "<html>EDL-MAINTENANCE-PAGE</html>"
        )

    monkeypatch.setattr("titiler.multidim.reader.guess_opener", raise_failure)
    response = app.get("/variables", params={"url": "s3://asdc-prod-protected/store"})
    assert response.status_code == 500
    assert "EDL-MAINTENANCE-PAGE" not in response.text
    assert "Earthdata Login" in response.text


def test_errors_not_cacheable(app):
    """Error responses must not carry Cache-Control, so CDNs never cache them."""
    err = app.get("/variables")  # missing required ?url= -> 422
    assert err.status_code == 422
    assert "cache-control" not in err.headers

    ok = app.get("/colorMaps")
    assert ok.status_code == 200
    assert ok.headers["cache-control"] == "public, max-age=3600"

    healthz = app.get("/healthz")
    assert healthz.status_code == 200
    assert "cache-control" not in healthz.headers
