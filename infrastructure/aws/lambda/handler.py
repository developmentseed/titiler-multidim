"""AWS Lambda handler optimized for container runtime with OTEL instrumentation."""

import logging
import warnings
from typing import Any, Dict

from mangum import Mangum
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor

from titiler.multidim.main import app

# Configure root logger to WARN level by default
# Use simple format - AWS Lambda will handle JSON formatting when AWS_LAMBDA_LOG_FORMAT=JSON
logging.basicConfig(
    level=logging.WARN,
    format="[%(levelname)s] %(name)s: %(message)s",
)

# Set titiler loggers to INFO level
logging.getLogger("titiler").setLevel(logging.INFO)

# Keep specific loggers at ERROR/WARNING levels
logging.getLogger("mangum.lifespan").setLevel(logging.ERROR)
logging.getLogger("mangum.http").setLevel(logging.ERROR)
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

LoggingInstrumentor().instrument(set_logging_format=False)
FastAPIInstrumentor.instrument_app(app)

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
