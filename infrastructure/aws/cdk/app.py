"""Construct App."""

import os
from typing import Any, Dict, Optional

from aws_cdk import (
    App,
    CfnOutput,
    Duration,
    PermissionsBoundary,
    Stack,
    Tags,
    aws_lambda,
)
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from constructs import Construct

from titiler.multidim.settings import AppSettings, StackSettings

stack_settings = StackSettings()
app_settings = AppSettings()

DEFAULT_ENV = {
    "GDAL_CACHEMAX": "200",  # 200 mb
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_INGESTED_BYTES_AT_OPEN": "32768",  # get more bytes when opening the files.
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_VERSION": "2",
    "PYTHONWARNINGS": "ignore",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "5000000",  # 5 MB (per file-handle)
    "AWS_EC2_METADATA_DISABLED": "true",
}


class LambdaStack(Stack):
    """Lambda Stack"""

    def __init__(
        self,
        scope: Construct,
        id: str,
        memory: int = 1024,
        timeout: int = 30,
        concurrent: Optional[int] = None,
        environment: Optional[Dict] = None,
        context_dir: str = "../../",
        **kwargs: Any,
    ) -> None:
        """Define stack."""
        super().__init__(scope, id, **kwargs)

        environment = environment or {}

        veda_reader_role = iam.Role.from_role_arn(
            self,
            "reader-role",
            role_arn=app_settings.reader_role_arn,
            mutable=False,
        )

        lambda_env = {
            **DEFAULT_ENV,
            **environment,
            "TITILER_MULTIDIM_ROOT_PATH": app_settings.root_path,
        }

        if app_settings.telemetry_enabled:
            lambda_env["TITILER_MULTIDIM_TELEMETRY_ENABLED"] = "TRUE"
            lambda_env["OTEL_SERVICE_NAME"] = stack_settings.titiler_multidim_stack_name

        lambda_function = aws_lambda.Function(
            self,
            f"{id}-lambda",
            runtime=aws_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=aws_lambda.Code.from_docker_build(
                path=os.path.abspath(context_dir),
                file="infrastructure/aws/lambda/Dockerfile",
                platform="linux/amd64",
            ),
            memory_size=memory,
            reserved_concurrent_executions=concurrent,
            timeout=Duration.seconds(timeout),
            environment=lambda_env,
            log_retention=logs.RetentionDays.ONE_WEEK,
            role=veda_reader_role,
            tracing=(
                aws_lambda.Tracing.ACTIVE
                if app_settings.telemetry_enabled
                else aws_lambda.Tracing.DISABLED
            ),
            snap_start=aws_lambda.SnapStartConf.ON_PUBLISHED_VERSIONS,
        )

        if app_settings.telemetry_enabled:
            lambda_function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=[
                        "xray:PutSpans",
                        "xray:PutSpansForIndexing",
                        "xray:PutTraceSegments",
                        "xray:PutTelemetryRecords",
                    ],
                    resources=["*"],
                )
            )

        # SnapStart only activates on published versions. Create a version and
        # alias so that API Gateway integrates with a versioned function rather
        # than $LATEST, which would bypass the snapshot entirely.
        live_alias = aws_lambda.Alias(
            self,
            f"{id}-live",
            alias_name="live",
            version=lambda_function.current_version,
        )

        api = apigw.HttpApi(
            self,
            f"{id}-endpoint",
            default_integration=HttpLambdaIntegration(
                f"{id}-integration",
                live_alias,
                parameter_mapping=apigw.ParameterMapping().overwrite_header(
                    "host",
                    apigw.MappingValue(stack_settings.veda_custom_host),
                )
                if stack_settings.veda_custom_host
                else None,
            ),
        )

        # Create an SNS Topic
        if app_settings.alarm_email:
            topic = sns.Topic(
                self,
                f"{id}-500-Errors",
                display_name=f"{id} Gateway 500 Errors",
                topic_name=f"{id}-Gateway-500-Errors",
            )
            # Subscribe email to the topic
            email_address = app_settings.alarm_email
            topic.add_subscription(subscriptions.EmailSubscription(email_address))

            # Create CloudWatch Alarm
            alarm = cloudwatch.Alarm(
                self,
                "MyAlarm",
                metric=cloudwatch.Metric(
                    namespace="AWS/ApiGateway",
                    metric_name="5XXError",
                    dimensions_map={"ApiName": f"{id}-endpoint"},
                    period=Duration.minutes(1),
                ),
                evaluation_periods=1,
                threshold=1,
                alarm_description="Alarm if 500 errors are detected",
                alarm_name=f"{id}-ApiGateway500Alarm",
                actions_enabled=True,
            )
            alarm.add_alarm_action(cloudwatch_actions.SnsAction(topic))
        CfnOutput(self, "Endpoint", value=api.url)


app = App()


lambda_stack = LambdaStack(
    app,
    f"{stack_settings.titiler_multidim_stack_name}-{stack_settings.stage}",
    memory=10240,
    timeout=app_settings.timeout,
    concurrent=app_settings.max_concurrent,
    environment=app_settings.additional_env,
lambda_stack = LambdaStack(
    app,
    f"{stack_settings.titiler_multidim_stack_name}-{stack_settings.stage}",
    memory=10240,
    timeout=app_settings.timeout,
    concurrent=app_settings.max_concurrent,
    environment=app_settings.additional_env,
    permissions_boundary=(
        PermissionsBoundary.from_name(stack_settings.permissions_boundary_policy_name)
        if stack_settings.permissions_boundary_policy_name
        else None
    ),
)
# Tag infrastructure
for key, value in {
    "Project": stack_settings.titiler_multidim_stack_name,
    "Stack": stack_settings.stage,
    "Owner": stack_settings.owner,
}.items():
    if value:
        Tags.of(lambda_stack).add(key, value)


app.synth()
