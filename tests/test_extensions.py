"""Custom extension tests."""

import pytest


def test_dataset_metadata_extension_uses_one_url(app):
    """Dataset metadata routes accept one URL without raster parameters."""
    params = {"url": "tests/fixtures/testfile.nc"}

    keys = app.get("/dataset/keys", params=params)
    metadata = app.get("/dataset/dict", params=params)
    html = app.get("/dataset/", params=params)
    validation = app.get("/validate", params=params)

    assert keys.status_code == 200
    assert keys.json() == ["data"]
    assert metadata.status_code == 200
    assert metadata.json()["data_vars"]["data"]
    assert html.status_code == 200
    assert validation.status_code == 200
    assert validation.json()["data"].keys() == {
        "compatible_with_titiler",
        "errors",
        "warnings",
    }


@pytest.mark.parametrize(
    "path", ["/dataset/", "/dataset/dict", "/dataset/keys", "/validate"]
)
def test_dataset_metadata_extension_rejects_multiple_urls(app, path):
    """Dataset metadata routes do not invent multi-source metadata."""
    response = app.get(
        path,
        params=[("url", "tests/fixtures/testfile.nc")] * 2,
    )

    assert response.status_code == 422
