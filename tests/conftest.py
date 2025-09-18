"""Auto-parametrized fixture that runs both cache configurations."""

import sys
import pytest
from fastapi.testclient import TestClient


# This fixture will automatically parametrize ALL tests that use it
@pytest.fixture(
    params=[
        pytest.param({"cache": True}, id="with_cache"),
        pytest.param({"cache": False}, id="without_cache"),
    ]
)
def app(request, monkeypatch):
    """Auto-parametrized app fixture that runs tests with both cache configurations."""
    config = request.param
    enable_cache = config.get("cache", False)

    # Set environment variables using monkeypatch (auto-cleanup)
    monkeypatch.setenv("TITILER_MULTIDIM_DEBUG", "TRUE")
    monkeypatch.setenv("TEST_ENVIRONMENT", "1")
    monkeypatch.setenv(
        "TITILER_MULTIDIM_ENABLE_CACHE", "TRUE" if enable_cache else "FALSE"
    )

    # Clear module cache to ensure fresh import
    modules_to_clear = [
        key for key in sys.modules.keys() if key.startswith("titiler.multidim")
    ]
    for module in modules_to_clear:
        del sys.modules[module]

    # Import and return the app
    from titiler.multidim.main import app

    with TestClient(app) as client:
        yield client
