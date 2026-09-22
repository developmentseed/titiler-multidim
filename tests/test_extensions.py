"""Custom extension tests."""

import pytest


def test_dataset_metadata_extension_uses_first_url(app):
    """Dataset metadata routes use the first of repeated source URLs."""
    params = [
        ("url", "tests/fixtures/testfile.nc"),
        ("url", "tests/fixtures/does-not-exist.nc"),
    ]

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
def test_dataset_metadata_extension_documents_repeated_urls(app, path):
    """Dataset metadata routes expose repeated url query parameters."""
    parameters = app.get("/api").json()["paths"][path]["get"]["parameters"]
    url_parameter = next(
        parameter for parameter in parameters if parameter["name"] == "url"
    )

    assert url_parameter["schema"]["type"] == "array"
    assert url_parameter["schema"]["items"]["type"] == "string"


def test_metadata_opener_uses_first_url_and_configured_authorization(app, monkeypatch):
    """The local opener passes the first URL and configured access to guess_opener."""
    from titiler.multidim import extensions

    captured = {}

    def fake_guess_opener(src_path, **kwargs):
        captured["src_path"] = src_path
        captured.update(kwargs)
        return "dataset"

    monkeypatch.setattr(extensions, "guess_opener", fake_guess_opener)

    assert (
        extensions.open_metadata_dataset(
            ["first.zarr", "second.zarr"], group="group", decode_times=False
        )
        == "dataset"
    )
    assert captured == {
        "src_path": "first.zarr",
        "group": "group",
        "decode_times": False,
        "authorize_virtual_chunk_access": extensions.api_settings.authorized_chunk_access,
    }
