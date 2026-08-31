"""Shared test fixtures."""

import json
import sys
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app(monkeypatch):
    """Return a test client for the application."""
    # Set environment variables using monkeypatch (auto-cleanup)
    monkeypatch.setenv("TITILER_MULTIDIM_DEBUG", "TRUE")
    # virtual container auth for icechunk tests
    monkeypatch.setenv(
        "TITILER_MULTIDIM_AUTHORIZED_CHUNK_ACCESS",
        json.dumps(
            {"s3://nasa-waterinsight/NLDAS3/forcing/daily/": {"anonymous": True}}
        ),
    )

    # Clear module cache to ensure fresh import
    modules_to_clear = [
        key for key in sys.modules.keys() if key.startswith("titiler.multidim")
    ]
    for module in modules_to_clear:
        del sys.modules[module]

    from titiler.multidim.main import app

    with TestClient(app) as client:
        yield client
