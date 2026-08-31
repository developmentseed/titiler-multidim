"""titiler.multidim."""

import logging
import os

import icechunk
import zarr
from earthaccess_auth.exceptions import (
    LoginAttemptFailure,
    LoginStrategyUnavailable,
    S3CredentialsRequestFailure,
)
from fastapi import FastAPI
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
from titiler.mosaic.errors import MOSAIC_STATUS_CODES

from titiler.multidim import __version__ as titiler_version
from titiler.multidim.extensions import DatasetMetadataExtension
from titiler.multidim.factory import XarrayMosaicTilerFactory
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
    # service misconfiguration (no EDL identity available); messages are
    # sanitized at the raise site (earthdata.py, earthaccess-auth)
    LoginStrategyUnavailable: status.HTTP_500_INTERNAL_SERVER_ERROR,
}
add_exception_handlers(app, error_codes)
add_exception_handlers(app, DEFAULT_STATUS_CODES)
add_exception_handlers(app, MOSAIC_STATUS_CODES)

logger = logging.getLogger(__name__)


def _sanitized_handler(status_code: int, detail: str, log_prefix: str):
    """Return a handler that logs str(exc) and serves a fixed message.

    The earthaccess-auth exceptions handled here embed raw upstream HTTP
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
_eula_403 = _sanitized_handler(
    status.HTTP_403_FORBIDDEN,
    "The data provider rejected the request for S3 credentials. This "
    "usually means an Earthdata Login EULA or application approval is "
    "missing: review "
    "https://urs.earthdata.nasa.gov/users/earthaccess/unaccepted_eulas "
    "and https://urs.earthdata.nasa.gov/application_search, then retry.",
    "DAAC s3credentials request rejected",
)
# 401 means the SERVICE's own EDL credentials were rejected (expired
# ~60-day token, bad secret): an operator problem that must alarm as a
# 5xx, not a 403 telling end users to accept EULAs they can't act on
_service_credentials_500 = _sanitized_handler(
    status.HTTP_500_INTERNAL_SERVER_ERROR,
    "The service's Earthdata Login credentials were rejected; see the "
    "service logs for details.",
    "service Earthdata credentials rejected by s3credentials endpoint",
)


def _handle_s3credentials_failure(request, exc):
    if getattr(exc, "status_code", None) == 401:
        return _service_credentials_500(request, exc)
    return _eula_403(request, exc)


app.add_exception_handler(S3CredentialsRequestFailure, _handle_s3credentials_failure)

# EDL rejected the service's own credentials (bad secret, EDL outage)
app.add_exception_handler(
    LoginAttemptFailure,
    _sanitized_handler(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "Authentication with Earthdata Login failed; see the service logs for details.",
        "Earthdata Login rejected the service credentials",
    ),
)

_icechunk_500 = _sanitized_handler(
    status.HTTP_500_INTERNAL_SERVER_ERROR,
    "icechunk storage error; see the service logs for details.",
    "icechunk storage error",
)


def _handle_icechunk_error(request, exc):
    """Sanitize icechunk errors, keeping credential failures identifiable.

    icechunk's Rust layer stringifies exceptions raised inside credential
    callables — including the refreshable-credential refresh it performs
    at chunk-read time, the steady state — chaining raw upstream response
    bodies into str(exc), so the text must never reach the client. The
    wrapped exception's type name is only present as text, hence the
    string matching; it keeps the EULA guidance (403) and the
    service-credential distinction (401 -> 500) working on that path.
    """
    text = str(exc)
    if "S3CredentialsRequestFailure" in text:
        if "status 401" in text:
            return _service_credentials_500(request, exc)
        return _eula_403(request, exc)
    return _icechunk_500(request, exc)


app.add_exception_handler(icechunk.IcechunkError, _handle_icechunk_error)

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
