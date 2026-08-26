"""titiler.multidim."""

import logging

import zarr
from earthaccess_auth.exceptions import (
    LoginAttemptFailure,
    LoginStrategyUnavailable,
    S3CredentialsRequestFailure,
)
from fastapi import Depends, FastAPI
from starlette import status
from starlette.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from titiler.core.errors import DEFAULT_STATUS_CODES, add_exception_handlers
from titiler.core.factory import AlgorithmFactory, ColorMapFactory, TMSFactory
from titiler.core.middleware import (
    CacheControlMiddleware,
    LoggerMiddleware,
    TotalTimeMiddleware,
)

from titiler.multidim import __version__ as titiler_version
from titiler.multidim.factory import XarrayTilerFactory
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
xarray_factory = XarrayTilerFactory(enable_telemetry=api_settings.telemetry_enabled)
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
    # service misconfiguration (no EDL identity available); messages are
    # sanitized at the raise site (earthdata.py, earthaccess-auth)
    LoginStrategyUnavailable: status.HTTP_500_INTERNAL_SERVER_ERROR,
}
add_exception_handlers(app, error_codes)
add_exception_handlers(app, DEFAULT_STATUS_CODES)

logger = logging.getLogger(__name__)


def _sanitized_handler(status_code: int, detail: str, log_prefix: str):
    """Return a handler that logs str(exc) and serves a fixed message.

    Both earthaccess-auth exceptions handled here embed raw upstream HTTP
    response bodies (DAAC error pages, EDL maintenance pages) in their
    message, so unlike the error_codes mapping above — which returns
    str(exc) — the exception text must never reach the client.
    """

    def handler(request, exc):
        logger.error("%s: %s", log_prefix, exc)
        return JSONResponse(status_code=status_code, content={"detail": detail})

    return handler


# EULA not accepted / DAAC rejected the credential request. The EDL pages
# listing pending EULAs and application approvals are stable well-known
# URLs, so point the caller there instead of echoing the DAAC's response.
app.add_exception_handler(
    S3CredentialsRequestFailure,
    _sanitized_handler(
        status.HTTP_403_FORBIDDEN,
        "The data provider rejected the request for S3 credentials. This "
        "usually means an Earthdata Login EULA or application approval is "
        "missing: review "
        "https://urs.earthdata.nasa.gov/users/earthaccess/unaccepted_eulas "
        "and https://urs.earthdata.nasa.gov/application_search, then retry.",
        "DAAC s3credentials request rejected",
    ),
)
# EDL rejected the service's own credentials (bad secret, EDL outage)
app.add_exception_handler(
    LoginAttemptFailure,
    _sanitized_handler(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "Authentication with Earthdata Login failed; see the service logs for details.",
        "Earthdata Login rejected the service credentials",
    ),
)

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
