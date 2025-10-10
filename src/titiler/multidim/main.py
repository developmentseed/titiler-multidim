"""titiler.multidim."""

import logging

import rioxarray
import zarr
from fastapi import Depends, FastAPI
from starlette import status
from starlette.middleware.cors import CORSMiddleware

import titiler.multidim.reader as reader
from titiler.core.errors import DEFAULT_STATUS_CODES, add_exception_handlers
from titiler.core.factory import AlgorithmFactory, ColorMapFactory, TMSFactory
from titiler.core.middleware import (
    CacheControlMiddleware,
    LoggerMiddleware,
    TotalTimeMiddleware,
)
from titiler.multidim import __version__ as titiler_version
from titiler.multidim.factory import XarrayTilerFactory
from titiler.multidim.middleware import ServerTimingMiddleware
from titiler.multidim.redis_pool import get_redis
from titiler.multidim.settings import ApiSettings

logging.getLogger("botocore.credentials").disabled = True
logging.getLogger("botocore.utils").disabled = True
logging.getLogger("rio-tiler").setLevel(logging.ERROR)

api_settings = ApiSettings()

app = FastAPI(
    title=api_settings.name,
    openapi_url="/api",
    docs_url="/api.html",
    version=titiler_version,
    root_path=api_settings.root_path,
)

###############################################################################
# Tiles endpoints
xarray_factory = XarrayTilerFactory()
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
    exclude_path={r"/healthz"},
)

app.add_middleware(LoggerMiddleware)

if api_settings.debug:
    app.add_middleware(TotalTimeMiddleware)
    app.add_middleware(
        ServerTimingMiddleware,
        calls_to_track={
            "1-xarray-open_dataset": (reader.xarray_open_dataset,),
            "2-rioxarray-reproject": (rioxarray.raster_array.RasterArray.reproject,),
        },
    )


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
