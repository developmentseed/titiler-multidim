"""AWS Lambda handler optimized for container runtime with OTEL instrumentation."""

import json
import logging
import os
import warnings
from datetime import datetime, timezone
from typing import Any, Dict

from mangum import Mangum

from titiler.multidim.main import app


def otel_trace_id_to_xray_format(otel_trace_id: str) -> str:
    """
    Convert OpenTelemetry trace ID to X-Ray format.

    OTEL format: 32 hex chars (e.g., "68eeb2ec45b07caf760899f308d34ab6")
    X-Ray format: "1-{first 8 chars}-{remaining 24 chars}" (e.g., "1-68eeb2ec-45b07caf760899f308d34ab6")

    The first 8 hex chars represent the Unix timestamp, which is how X-Ray generates compatible IDs.
    """
    if len(otel_trace_id) == 32:
        return f"1-{otel_trace_id[:8]}-{otel_trace_id[8:]}"
    return otel_trace_id


class XRayJsonFormatter(logging.Formatter):
    """
    Custom JSON formatter that includes X-Ray trace ID for log correlation.

    This formatter outputs logs as JSON and includes:
    - Standard log fields (timestamp, level, message, logger)
    - X-Ray trace ID (converted from OTEL format)
    - OTEL trace context fields (if present)
    - Any extra fields passed via logger.info("msg", extra={...})
    """

    # Standard fields that shouldn't be duplicated in the output
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
        # Build base log object with standard fields
        log_object = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }

        # Add X-Ray trace ID
        xray_trace_id = None

        # Method 1: Extract from Lambda's X-Ray environment variable (preferred)
        trace_header = os.environ.get("_X_AMZN_TRACE_ID", "")
        if trace_header:
            for part in trace_header.split(";"):
                if part.startswith("Root="):
                    xray_trace_id = part.split("=", 1)[1]
                    break

        # Method 2: Convert OTEL trace ID if available (fallback)
        if not xray_trace_id and hasattr(record, "otelTraceID"):
            xray_trace_id = otel_trace_id_to_xray_format(record.otelTraceID)

        if xray_trace_id:
            log_object["xray_trace_id"] = xray_trace_id

        # Add exception info if present
        if record.exc_info:
            log_object["exception"] = self.formatException(record.exc_info)

        # Add OTEL fields if present
        for attr in [
            "otelSpanID",
            "otelTraceID",
            "otelTraceSampled",
            "otelServiceName",
        ]:
            if hasattr(record, attr):
                log_object[attr] = getattr(record, attr)

        # Add AWS request ID if available
        if hasattr(record, "aws_request_id"):
            log_object["requestId"] = record.aws_request_id

        # Add any extra fields from record.__dict__ that aren't standard
        for key, value in record.__dict__.items():
            if key not in self.RESERVED_ATTRS and key not in log_object:
                log_object[key] = value

        return json.dumps(log_object)


# Configure root logger with custom JSON formatter that includes X-Ray trace ID
root_logger = logging.getLogger()
root_logger.setLevel(logging.WARN)

# Remove any existing handlers
for log_handler in root_logger.handlers[:]:
    root_logger.removeHandler(log_handler)

# Add StreamHandler with our custom JSON formatter
json_handler = logging.StreamHandler()
json_handler.setFormatter(XRayJsonFormatter())
root_logger.addHandler(json_handler)

# Set titiler loggers to INFO level
logging.getLogger("titiler").setLevel(logging.INFO)

# Keep specific loggers at ERROR/WARNING levels
logging.getLogger("mangum.lifespan").setLevel(logging.ERROR)
logging.getLogger("mangum.http").setLevel(logging.ERROR)
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

# LoggingInstrumentor().instrument(set_logging_format=False)
# FastAPIInstrumentor.instrument_app(app)

handler = Mangum(
    app,
    lifespan="off",
    api_gateway_base_path=None,
    text_mime_types=[
        "application/json",
        "application/javascript",
        "application/xml",
        "application/vnd.api+json",
    ],
)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Lambda handler with container-specific optimizations and OTEL tracing."""
    return handler(event, context)
