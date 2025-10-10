"""AWS Lambda handler optimized for container runtime."""

import logging
import warnings
from typing import Any, Dict

# Initialize OpenTelemetry BEFORE importing the FastAPI app
from otel_config import setup_otel

setup_otel()

from mangum import Mangum  # noqa: E402

from titiler.multidim.main import app  # noqa: E402

# Configure root logger to WARN level by default
logging.basicConfig(
    level=logging.WARN,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
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


# Pre-import commonly used modules for faster cold starts
try:
    import numpy  # noqa: F401
    import pandas  # noqa: F401
    import rioxarray  # noqa: F401
    import xarray  # noqa: F401
except ImportError:
    pass

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
    """Lambda handler with container-specific optimizations."""
    response = handler(event, context)

    return response


handler.lambda_handler = lambda_handler
