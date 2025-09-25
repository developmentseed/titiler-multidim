"""AWS Lambda handler optimized for container runtime."""

import logging
import os
import warnings
from typing import Any, Dict

from mangum import Mangum

from titiler.multidim.main import app

# Configure logging for Lambda CloudWatch integration
logging.getLogger("mangum.lifespan").setLevel(logging.ERROR)
logging.getLogger("mangum.http").setLevel(logging.ERROR)
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Suppress warnings for cleaner CloudWatch logs
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

# Lambda container optimizations
os.environ.setdefault("PYTHONPATH", "/var/runtime")
os.environ.setdefault("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-west-2"))

# Pre-import commonly used modules for faster cold starts
try:
    import numpy  # noqa: F401
    import pandas  # noqa: F401
    import rioxarray  # noqa: F401
    import xarray  # noqa: F401
except ImportError:
    pass

# Initialize Mangum with optimizations for Lambda containers
handler = Mangum(
    app,
    lifespan="off",  # Disable lifespan for Lambda
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
    # Handle the request
    response = handler(event, context)

    # Optional: Force garbage collection for memory management
    # Uncomment if experiencing memory issues
    # gc.collect()

    return response


# Alias for backward compatibility and direct Mangum usage
handler.lambda_handler = lambda_handler
