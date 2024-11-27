"""titiler.xarray tests configuration."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app(monkeypatch):
    """App fixture."""
    monkeypatch.setenv("TITILER_XARRAY_DEBUG", "TRUE")
    monkeypatch.setenv("TEST_ENVIRONMENT", "1")

    from titiler.multidim.main import app

    with TestClient(app) as client:
        yield client
