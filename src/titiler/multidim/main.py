"""titiler.multidim."""

import logging
import os
from typing import Annotated, Literal

import icechunk
import jinja2
import zarr
from earthaccess_auth.exceptions import (
    LoginAttemptFailure,
    LoginStrategyUnavailable,
    S3CredentialsRequestFailure,
)
from fastapi import FastAPI, Query
from starlette import status
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.templating import Jinja2Templates
from titiler.core.errors import DEFAULT_STATUS_CODES, add_exception_handlers
from titiler.core.factory import AlgorithmFactory, ColorMapFactory, TMSFactory
from titiler.core.middleware import (
    CacheControlMiddleware,
    LoggerMiddleware,
    TotalTimeMiddleware,
)
from titiler.core.models.OGC import Conformance, Landing
from titiler.core.resources.enums import MediaType
from titiler.core.utils import accept_media_type, create_html_response
from titiler.mosaic.errors import MOSAIC_STATUS_CODES
from titiler.xarray.extensions import DatasetMetadataExtension, ValidateExtension

from titiler.multidim import __version__ as titiler_version
from titiler.multidim.extensions import open_metadata_dataset
from titiler.multidim.factory import XarrayMosaicTilerFactory
from titiler.multidim.reader import WhereConditionError
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

# local landing.html, then titiler.xarray's header (navbar) and
# conformance.html, then the titiler.core defaults
templates = Jinja2Templates(
    env=jinja2.Environment(
        autoescape=jinja2.select_autoescape(["html", "xml"]),
        loader=jinja2.ChoiceLoader(
            [
                jinja2.PackageLoader(__package__, "templates"),
                jinja2.PackageLoader("titiler.xarray", "templates"),
                jinja2.PackageLoader("titiler.core", "templates"),
            ]
        ),
    )
)

TITILER_CONFORMS_TO = {
    "http://www.opengis.net/spec/ogcapi-common-1/1.0/conf/core",
    "http://www.opengis.net/spec/ogcapi-common-1/1.0/conf/landing-page",
    "http://www.opengis.net/spec/ogcapi-common-1/1.0/conf/oas30",
    "http://www.opengis.net/spec/ogcapi-common-1/1.0/conf/html",
    "http://www.opengis.net/spec/ogcapi-common-1/1.0/conf/json",
}

###############################################################################
# Tiles endpoints
xarray_factory = XarrayMosaicTilerFactory(
    enable_telemetry=api_settings.telemetry_enabled,
    extensions=[
        DatasetMetadataExtension(dataset_opener=open_metadata_dataset),
        ValidateExtension(dataset_opener=open_metadata_dataset),
    ],
    templates=templates,
)
app.include_router(xarray_factory.router, tags=["Xarray Tiler API"])
TITILER_CONFORMS_TO.update(xarray_factory.conforms_to)

###############################################################################
# TileMatrixSets endpoints
tms = TMSFactory(templates=templates)
app.include_router(tms.router, tags=["Tiling Schemes"])
TITILER_CONFORMS_TO.update(tms.conforms_to)

###############################################################################
# Algorithms endpoints
algorithms = AlgorithmFactory(templates=templates)
app.include_router(algorithms.router, tags=["Algorithms"])
TITILER_CONFORMS_TO.update(algorithms.conforms_to)

###############################################################################
# Colormaps endpoints
cmaps = ColorMapFactory(templates=templates)
app.include_router(
    cmaps.router,
    tags=["ColorMaps"],
)
TITILER_CONFORMS_TO.update(cmaps.conforms_to)

error_codes = {
    zarr.errors.GroupNotFoundError: status.HTTP_422_UNPROCESSABLE_ENTITY,
    WhereConditionError: status.HTTP_400_BAD_REQUEST,
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


def _output_type(request: Request, f: str | None) -> MediaType:
    """Pick html or json from ?f=, else the Accept header, else json."""
    if f:
        return MediaType[f]
    return (
        accept_media_type(
            request.headers.get("accept", ""), [MediaType.html, MediaType.json]
        )
        or MediaType.json
    )


FormatQuery = Annotated[
    Literal["html", "json"] | None,
    Query(
        description="Response MediaType. Defaults to endpoint's default or value defined in `accept` header."
    ),
]


@app.get(
    "/",
    response_model=Landing,
    response_model_exclude_none=True,
    responses={200: {"content": {"text/html": {}, "application/json": {}}}},
    tags=["OGC Common"],
)
def landing(request: Request, f: FormatQuery = None):
    """TiTiler landing page."""
    data = {
        "title": api_settings.name,
        "description": "Dynamic tiles for multidimensional (Zarr, NetCDF, icechunk) datasets, built on titiler.xarray.",
        "links": [
            {
                "title": "Landing page",
                "href": str(request.url_for("landing")),
                "type": "text/html",
                "rel": "self",
            },
            {
                "title": "The API definition (JSON)",
                "href": str(request.url_for("openapi")),
                "type": "application/vnd.oai.openapi+json;version=3.0",
                "rel": "service-desc",
            },
            {
                "title": "The API documentation",
                "href": str(request.url_for("swagger_ui_html")),
                "type": "text/html",
                "rel": "service-doc",
            },
            {
                "title": "Conformance Declaration",
                "href": str(request.url_for("conformance")),
                "type": "text/html",
                "rel": "http://www.opengis.net/def/rel/ogc/1.0/conformance",
            },
            {
                "title": "Map viewer",
                "href": str(
                    request.url_for("map_viewer", tileMatrixSetId="WebMercatorQuad")
                ),
                "type": "text/html",
                "rel": "data",
            },
            {
                "title": "List of Available TileMatrixSets",
                "href": str(request.url_for("tilematrixsets")),
                "type": "application/json",
                "rel": "http://www.opengis.net/def/rel/ogc/1.0/tiling-schemes",
            },
            {
                "title": "List of Available Algorithms",
                "href": str(request.url_for("available_algorithms")),
                "type": "application/json",
                "rel": "data",
            },
            {
                "title": "List of Available ColorMaps",
                "href": str(request.url_for("available_colormaps")),
                "type": "application/json",
                "rel": "data",
            },
            {
                "title": "TiTiler Documentation (external link)",
                "href": "https://developmentseed.org/titiler/",
                "type": "text/html",
                "rel": "doc",
            },
            {
                "title": "titiler-multidim source code (external link)",
                "href": "https://github.com/developmentseed/titiler-multidim",
                "type": "text/html",
                "rel": "doc",
            },
        ],
    }

    if _output_type(request, f) == MediaType.html:
        return create_html_response(
            request,
            data,
            title=api_settings.name,
            template_name="landing",
            templates=templates,
        )
    return data


@app.get(
    "/conformance",
    response_model=Conformance,
    response_model_exclude_none=True,
    responses={200: {"content": {"text/html": {}, "application/json": {}}}},
    tags=["OGC Common"],
)
def conformance(request: Request, f: FormatQuery = None):
    """Conformance classes.

    Called with `GET /conformance`.

    """
    data = {"conformsTo": sorted(TITILER_CONFORMS_TO)}

    if _output_type(request, f) == MediaType.html:
        return create_html_response(
            request,
            data,
            title="Conformance",
            template_name="conformance",
            templates=templates,
        )
    return data


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
