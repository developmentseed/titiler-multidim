"""titiler.multidim."""

import logging
import os

import zarr
from fastapi import Depends, FastAPI
from starlette import status
from starlette.middleware.cors import CORSMiddleware
from titiler.core.errors import DEFAULT_STATUS_CODES, add_exception_handlers
from titiler.core.factory import AlgorithmFactory, ColorMapFactory, TMSFactory
from titiler.core.middleware import (
    CacheControlMiddleware,
    LoggerMiddleware,
    TotalTimeMiddleware,
)

from titiler.multidim import __version__ as titiler_version
from titiler.multidim.extensions import DatasetMetadataExtension
from titiler.multidim.factory import XarrayMosaicTilerFactory
from titiler.multidim.redis_pool import get_redis
from titiler.multidim.settings import ApiSettings

logging.getLogger("botocore.credentials").disabled = True
logging.getLogger("botocore.utils").disabled = True
logging.getLogger("rio_tiler").setLevel(logging.INFO)

api_settings = ApiSettings()

if "AWS_EXECUTION_ENV" not in os.environ:
    logging.basicConfig(
        level=logging.DEBUG if api_settings.debug else logging.INFO,
    )

app = FastAPI(
    title=api_settings.name,
    openapi_url="/api",
    docs_url="/api.html",
    version=titiler_version,
    root_path=api_settings.root_path,
)

###############################################################################
# Tiles endpoints
xarray_factory = XarrayMosaicTilerFactory(
    enable_telemetry=api_settings.telemetry_enabled,
    extensions=[DatasetMetadataExtension()],
)
app.include_router(xarray_factory.router, tags=["Xarray Tiler API"])

###############################################################################
# TileMatrixSets endpoints
tms = TMSFactory()
app.include_router(tms.router, tags=["Tiling Schemes"])

###############################################################################
# Algorithms endpoints
algorithms = AlgorithmFactory()
app.include_router(algorithms.router, tags=["Algorithms"])

###############################################################################
# Colormaps endpoints
cmaps = ColorMapFactory()
app.include_router(
    cmaps.router,
    tags=["ColorMaps"],
)

error_codes = {
    zarr.errors.GroupNotFoundError: status.HTTP_422_UNPROCESSABLE_ENTITY,
}
add_exception_handlers(app, error_codes)
add_exception_handlers(app, DEFAULT_STATUS_CODES)

# Set all CORS enabled origins
if api_settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=api_settings.cors_origins,
        allow_credentials=True,
        allow_methods=api_settings.cors_allow_methods,
        allow_headers=["*"],
    )

app.add_middleware(
    CacheControlMiddleware,
    cachecontrol=api_settings.cachecontrol,
    cachecontrol_max_http_code=400,  # never let CDNs cache error responses
    exclude_path={r"/healthz"},
)

app.add_middleware(LoggerMiddleware)

if api_settings.debug:
    app.add_middleware(TotalTimeMiddleware)


@app.get(
    "/healthz",
    description="Health Check.",
    summary="Health Check.",
    operation_id="healthCheck",
    tags=["Health Check"],
)
def ping():
    """Health check."""
    return {"ping": "pong!"}


@app.get("/clear_cache")
def clear_cache(cache_client=Depends(get_redis)):
    """Clear the cache."""
    cache_client.flushall()
    return {"status": "cache cleared!"}


if __name__ == "__main__":
    import uvicorn

    log_level = "debug" if api_settings.debug else "info"
    uvicorn.run(
        "titiler.multidim.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level=log_level,
    )
