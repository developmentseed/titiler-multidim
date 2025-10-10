"""OpenTelemetry configuration for AWS Lambda with X-Ray tracing."""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def setup_otel() -> None:
    """
    Configure OpenTelemetry with direct OTLP export to AWS X-Ray.

    This function:
    - Checks if tracing is enabled via OTEL_TRACES_ENABLED environment variable
    - Configures AWS X-Ray ID generator for proper trace ID format
    - Sets up OTLP HTTP exporter pointing to X-Ray service endpoint
    - Configures TracerProvider with BatchSpanProcessor
    - Auto-instruments: FastAPI, logging, botocore, and requests
    - Adds Lambda context attributes to traces

    Should be called once at module initialization (cold start).
    """
    # Check if tracing is enabled
    traces_enabled = os.environ.get("OTEL_TRACES_ENABLED", "false").lower() == "true"

    if not traces_enabled:
        logger.info("OpenTelemetry tracing is disabled (OTEL_TRACES_ENABLED != true)")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.logging import LoggingInstrumentor
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        from opentelemetry.propagate import set_global_textmap
        from opentelemetry.propagators.aws import AwsXRayPropagator
        from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        # Get Lambda context from environment
        function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "unknown")
        function_version = os.environ.get("AWS_LAMBDA_FUNCTION_VERSION", "unknown")
        log_stream_name = os.environ.get("AWS_LAMBDA_LOG_STREAM_NAME", "unknown")

        # Create resource with Lambda context attributes
        resource = Resource.create(
            {
                "service.name": function_name,
                "service.version": function_version,
                "cloud.provider": "aws",
                "cloud.platform": "aws_lambda",
                "faas.name": function_name,
                "faas.version": function_version,
                "aws.log.stream.name": log_stream_name,
            }
        )

        # Configure TracerProvider with AWS X-Ray ID generator
        tracer_provider = TracerProvider(
            resource=resource,
            id_generator=AwsXRayIdGenerator(),
        )

        # Configure OTLP HTTP exporter for X-Ray
        # X-Ray OTLP endpoint is region-specific
        region = os.environ.get("AWS_REGION", "us-east-1")
        otlp_endpoint = f"https://xray.{region}.amazonaws.com"

        otlp_exporter = OTLPSpanExporter(
            endpoint=f"{otlp_endpoint}/v1/traces",
            timeout=30,  # 30 second timeout for export
        )

        # Add BatchSpanProcessor for efficient span export
        span_processor = BatchSpanProcessor(
            otlp_exporter,
            max_queue_size=2048,
            max_export_batch_size=512,
            schedule_delay_millis=5000,  # Export every 5 seconds
        )
        tracer_provider.add_span_processor(span_processor)

        # Set global tracer provider
        trace.set_tracer_provider(tracer_provider)

        # Set AWS X-Ray propagator for trace context propagation
        set_global_textmap(AwsXRayPropagator())

        # Auto-instrument libraries
        FastAPIInstrumentor().instrument()
        LoggingInstrumentor().instrument(set_logging_format=True)
        BotocoreInstrumentor().instrument()
        RequestsInstrumentor().instrument()

        logger.info(
            "OpenTelemetry configured successfully with X-Ray export to %s",
            otlp_endpoint,
        )

    except Exception as e:
        # Log error but don't fail the Lambda cold start
        logger.error("Failed to configure OpenTelemetry: %s", e, exc_info=True)


def get_lambda_context_attributes(context: Optional[object] = None) -> dict:
    """
    Extract Lambda context attributes for tracing.

    Args:
        context: Lambda context object passed to handler

    Returns:
        Dictionary of Lambda context attributes
    """
    attributes = {}

    if context:
        if hasattr(context, "aws_request_id"):
            attributes["faas.execution"] = context.aws_request_id
        if hasattr(context, "invoked_function_arn"):
            attributes["faas.id"] = context.invoked_function_arn
        if hasattr(context, "memory_limit_in_mb"):
            attributes["faas.max_memory"] = int(context.memory_limit_in_mb)

    return attributes
