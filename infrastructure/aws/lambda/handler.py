"""AWS Lambda handler for zip deployment with OTEL/X-Ray instrumentation."""

import json
import logging
import os
import warnings
from datetime import datetime, timezone
from typing import Any, Dict

# Must be set before any import that triggers GDAL or PROJ context creation.
# GDAL_DATA: schema/SRS files from our custom GDAL build (bundled at /var/task/gdal_data).
# PROJ_DATA: CRS database (proj.db) bundled at /var/task/pyproj/proj_dir/share/proj.
os.environ.setdefault("GDAL_DATA", "/var/task/gdal_data")
os.environ.setdefault("PROJ_DATA", "/var/task/pyproj/proj_dir/share/proj")

import pyproj
import rasterio
from mangum import Mangum

from titiler.multidim.main import app
from titiler.multidim.settings import ApiSettings

api_settings = ApiSettings()


def otel_trace_id_to_xray_format(otel_trace_id: str) -> str:
    """Convert OpenTelemetry trace ID to X-Ray format.

    OTEL format: 32 hex chars (e.g., "68eeb2ec45b07caf760899f308d34ab6")
    X-Ray format: "1-{first 8 chars}-{remaining 24 chars}"
    """
    if len(otel_trace_id) == 32:
        return f"1-{otel_trace_id[:8]}-{otel_trace_id[8:]}"
    return otel_trace_id


class XRayJsonFormatter(logging.Formatter):
    """Custom JSON formatter that includes X-Ray trace ID for log correlation."""

    RESERVED_ATTRS = {
        "name",
        "msg",
        "args",
        "created",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "thread",
        "threadName",
        "exc_info",
        "exc_text",
        "stack_info",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:  # noqa: C901
        """Format log record as JSON with X-Ray trace ID."""
        log_object = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }

        xray_trace_id = None

        trace_header = os.environ.get("_X_AMZN_TRACE_ID", "")
        if trace_header:
            for part in trace_header.split(";"):
                if part.startswith("Root="):
                    xray_trace_id = part.split("=", 1)[1]
                    break

        if not xray_trace_id and hasattr(record, "otelTraceID"):
            xray_trace_id = otel_trace_id_to_xray_format(record.otelTraceID)

        if xray_trace_id:
            log_object["xray_trace_id"] = xray_trace_id

        if record.exc_info:
            log_object["exception"] = self.formatException(record.exc_info)

        for attr in [
            "otelSpanID",
            "otelTraceID",
            "otelTraceSampled",
            "otelServiceName",
        ]:
            if hasattr(record, attr):
                log_object[attr] = getattr(record, attr)

        if hasattr(record, "aws_request_id"):
            log_object["requestId"] = record.aws_request_id

        for key, value in record.__dict__.items():
            if key not in self.RESERVED_ATTRS and key not in log_object:
                log_object[key] = value

        return json.dumps(log_object)


root_logger = logging.getLogger()
root_logger.setLevel(logging.WARN)
for log_handler in root_logger.handlers[:]:
    root_logger.removeHandler(log_handler)
json_handler = logging.StreamHandler()
json_handler.setFormatter(XRayJsonFormatter())
root_logger.addHandler(json_handler)

logging.getLogger("titiler").setLevel(logging.INFO)
logging.getLogger("mangum.lifespan").setLevel(logging.ERROR)
logging.getLogger("mangum.http").setLevel(logging.ERROR)
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("numexpr").setLevel(logging.WARNING)

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

# Configure OTEL with X-Ray when running in Lambda and telemetry is enabled.
# Skipped outside Lambda so local dev/testing is unaffected.
if "AWS_EXECUTION_ENV" in os.environ and api_settings.telemetry_enabled:
    import hashlib
    from urllib.parse import urlparse

    import botocore.auth
    import botocore.awsrequest
    import requests
    from botocore.session import Session as BotocoreSession
    from opentelemetry import propagate, trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.propagators.aws import AwsXRayPropagator
    from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    _region = os.environ.get("AWS_REGION", "us-east-1")

    class _SigV4Auth(requests.auth.AuthBase):
        """Sign OTLP HTTP requests with AWS SigV4.

        A fresh BotocoreSession is created on every request so that Lambda
        credential rotations are always picked up and the full provider chain
        is available as a fallback (e.g. during SnapStart init before env vars
        are injected).
        """

        def __init__(self, service: str, region: str) -> None:
            self._service = service
            self._region = region

        def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
            """Add SigV4 Authorization header to the request."""
            credentials = BotocoreSession().get_credentials()
            if credentials is None:
                raise RuntimeError("AWS credentials are required to sign OTLP requests")
            frozen_credentials = credentials.get_frozen_credentials()
            parsed = urlparse(r.url)
            body = r.body if r.body is not None else b""
            body_bytes = body if isinstance(body, bytes) else body.encode("utf-8")
            body_hash = hashlib.sha256(body_bytes).hexdigest()

            sign_headers: dict[str, str] = {
                "Content-Type": str(
                    r.headers.get("Content-Type", "application/x-protobuf")
                ),
                "Host": str(parsed.hostname or ""),
                "X-Amz-Content-Sha256": body_hash,
            }
            if "Content-Encoding" in r.headers:
                sign_headers["Content-Encoding"] = str(r.headers["Content-Encoding"])

            aws_req = botocore.awsrequest.AWSRequest(
                method=r.method,
                url=r.url,
                data=body_bytes,
                headers=sign_headers,
            )
            botocore.auth.SigV4Auth(
                frozen_credentials, self._service, self._region
            ).add_auth(aws_req)
            for key in (
                "Authorization",
                "X-Amz-Date",
                "X-Amz-Security-Token",
                "X-Amz-Content-Sha256",
            ):
                if key in aws_req.headers:
                    r.headers[key] = aws_req.headers[key]
            return r

    _exporter = OTLPSpanExporter(
        endpoint=f"https://xray.{_region}.amazonaws.com/v1/traces",
    )
    _exporter._session.auth = _SigV4Auth("xray", _region)
    # After SnapStart restore the session's connection pool contains stale TCP
    # connections. requests does not retry POSTs by default, so a stale
    # connection causes the first post-restore export to fail silently.
    _exporter._session.mount(
        "https://",
        HTTPAdapter(max_retries=Retry(total=2, allowed_methods={"POST"})),
    )

    def _log_otlp_response(r: requests.Response, *args, **kwargs) -> None:
        """Log non-2xx OTLP export responses to diagnose signing/auth failures."""
        if r.status_code >= 300:
            logging.getLogger(__name__).error(
                "OTLP export failed: status=%s body=%r signed_headers=%s",
                r.status_code,
                r.text[:500],
                r.request.headers.get("Authorization", "")[:200],
            )

    _exporter._session.hooks["response"].append(_log_otlp_response)

    class _LambdaXRayPropagator(AwsXRayPropagator):
        """AwsXRayPropagator that falls back to the Lambda _X_AMZN_TRACE_ID env var.

        API Gateway HTTP API (v2) does not inject X-Amzn-Trace-Id into the
        Lambda event headers, so the standard propagator finds nothing and
        creates a new root span with a fresh trace ID. Reading from the env
        var ensures OTEL continues the same trace as Lambda's native X-Ray
        segment, keeping trace IDs consistent between OTEL spans, CloudWatch
        log entries, and the X-Ray console.
        """

        def extract(self, carrier, context=None, getter=None):
            """Extract trace context, falling back to _X_AMZN_TRACE_ID."""
            ctx = super().extract(carrier, context=context, getter=getter)
            if not trace.get_current_span(context=ctx).get_span_context().is_valid:
                trace_header = os.environ.get("_X_AMZN_TRACE_ID", "")
                if trace_header:
                    ctx = super().extract(
                        {"X-Amzn-Trace-Id": trace_header},
                        context=context,
                    )
            return ctx

    # AwsXRayIdGenerator produces trace IDs whose first 8 hex chars encode the
    # epoch timestamp — the format X-Ray requires for time-indexed trace lookup.
    # BatchSpanProcessor (not Simple) is used so span exports happen on a
    # background thread and never block the asyncio event loop.
    _provider = TracerProvider(id_generator=AwsXRayIdGenerator())
    _provider.add_span_processor(BatchSpanProcessor(_exporter))
    trace.set_tracer_provider(_provider)
    propagate.set_global_textmap(_LambdaXRayPropagator())

    LoggingInstrumentor().instrument(set_logging_format=True)
    HTTPXClientInstrumentor().instrument()
    FastAPIInstrumentor.instrument_app(app)

# ── SnapStart pre-warming ──────────────────────────────────────────────────────
# Build the Starlette/OTEL middleware stack now so the first post-restore request
# doesn't pay the assembly cost.
app.middleware_stack = app.build_middleware_stack()

# Open a rasterio.Env and leave it open (do not call __exit__). This calls
# gdal.AllRegister() and keeps the reference count at 1 so the GDAL driver
# manager is not torn down between invocations.
_gdal_env = rasterio.Env()
_gdal_env.__enter__()

# Warm the PROJ CRS cache so the first CRS lookup after restore hits the
# in-memory cache rather than the SQLite database.
pyproj.CRS.from_epsg(4326)
# ──────────────────────────────────────────────────────────────────────────────

_mangum = Mangum(
    app,
    lifespan="off",
    text_mime_types=[
        "application/json",
        "application/javascript",
        "application/xml",
        "application/vnd.api+json",
    ],
)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Lambda entry point with explicit OTEL flush after each invocation.

    BatchSpanProcessor exports spans on a background thread during the
    request. We flush here — after the response is fully assembled but
    before returning — so spans are not silently dropped when Lambda
    freezes the execution environment between invocations.

    The flush timeout (5 s) is intentionally shorter than the Lambda
    timeout so a slow X-Ray endpoint cannot push the invocation over
    the function's configured limit.
    """
    result = _mangum(event, context)
    if api_settings.telemetry_enabled:
        _provider.force_flush(timeout_millis=5_000)
    return result
