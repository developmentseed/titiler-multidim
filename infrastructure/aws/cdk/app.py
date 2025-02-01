"""Construct App."""

import os
from typing import Any, Dict, List, Optional

from aws_cdk import App, CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_apigatewayv2_alpha as apigw
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticache as elasticache
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk.aws_apigatewayv2_integrations_alpha import HttpLambdaIntegration
from config import StackSettings
from constructs import Construct

settings = StackSettings()


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
}


class LambdaStack(Stack):
    """Lambda Stack"""

    def __init__(
        self,
        scope: Construct,
        id: str,
        memory: int = 1024,
        timeout: int = 30,
        runtime: aws_lambda.Runtime = aws_lambda.Runtime.PYTHON_3_10,
        concurrent: Optional[int] = None,
        permissions: Optional[List[iam.PolicyStatement]] = None,
        environment: Optional[Dict] = None,
        context_dir: str = "../../",
        **kwargs: Any,
    ) -> None:
        """Define stack."""
        super().__init__(scope, id, *kwargs)

        permissions = permissions or []
        environment = environment or {}

        if settings.vpc_id:
            vpc = ec2.Vpc.from_lookup(
                self,
                f"{id}-vpc",
                vpc_id=settings.vpc_id,
                owner_account_id=Stack.of(self).account,
                region=Stack.of(self).region,
            )
        else:
            vpc = ec2.Vpc(
                self,
                f"{id}-vpc",
                max_azs=2,
                subnet_configuration=[
                    ec2.SubnetConfiguration(
                        name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                    )
                ],
            )

        security_group = ec2.SecurityGroup(
            self,
            "ElastiCacheSecurityGroup",
            vpc=vpc,
            description="Allow local access to ElastiCache redis",
            allow_all_outbound=True,
        )
        security_group.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(6379)
        )

        # Create the redis cluster
        redis_cluster = elasticache.CfnCacheCluster(
            self,
            f"{id}-redis-cluster",
            engine="redis",
            cache_node_type="cache.t3.small",
            num_cache_nodes=1,
            vpc_security_group_ids=[security_group.security_group_id],
            cache_subnet_group_name=f"{id}-cache-subnet-group",
            cluster_name=f"{id}-redis-cluster",
        )

        # Define the subnet group for the ElastiCache cluster
        subnet_group = elasticache.CfnSubnetGroup(
            self,
            f"{id}-cache-subnet-group",
            description="Subnet group for ElastiCache",
            subnet_ids=vpc.select_subnets(subnet_type=ec2.SubnetType.PUBLIC).subnet_ids,
            cache_subnet_group_name=f"{id}-cache-subnet-group",
        )

        # Add dependency - ensure subnet group is created before the cache cluster
        redis_cluster.add_depends_on(subnet_group)

        veda_reader_role = iam.Role.from_role_arn(
            self,
            "reader-role",
            role_arn=settings.reader_role_arn,
        )

        lambda_function = aws_lambda.Function(
            self,
            f"{id}-lambda",
            runtime=runtime,
            code=aws_lambda.Code.from_docker_build(
                path=os.path.abspath(context_dir),
                file="infrastructure/aws/lambda/Dockerfile",
                platform="linux/amd64",
                build_args={
                    "PYTHON_VERSION": runtime.to_string().replace("python", ""),
                },
            ),
            handler="handler.handler",
            memory_size=memory,
            reserved_concurrent_executions=concurrent,
            timeout=Duration.seconds(timeout),
            environment={
                **DEFAULT_ENV,
                **environment,
                "TITILER_XARRAY_CACHE_HOST": redis_cluster.attr_redis_endpoint_address,
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            allow_public_subnet=True,
            role=veda_reader_role,
        )

        # Create an S3 VPC Endpoint
        ec2.GatewayVpcEndpoint(
            self,
            f"{id}-s3-vpc-endpoint",
            vpc=vpc,
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )

        for perm in permissions:
            lambda_function.add_to_role_policy(perm)

        api = apigw.HttpApi(
            self,
            f"{id}-endpoint",
            default_integration=HttpLambdaIntegration(
                f"{id}-integration", lambda_function
            ),
        )

        # Create an SNS Topic
        if settings.alarm_email:
            topic = sns.Topic(
                self,
                f"{id}-500-Errors",
                display_name=f"{id} Gateway 500 Errors",
                topic_name=f"{id}-Gateway-500-Errors",
            )
            # Subscribe email to the topic
            email_address = settings.alarm_email
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

perms = []
if settings.buckets:
    perms.append(
        iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[f"arn:aws:s3:::{bucket}*" for bucket in settings.buckets],
        )
    )


lambda_stack = LambdaStack(
    app,
    f"{settings.name}-{settings.stage}",
    memory=10240,
    timeout=settings.timeout,
    concurrent=settings.max_concurrent,
    permissions=perms,
    environment=settings.additional_env,
)
# Tag infrastructure
for key, value in {
    "Project": settings.name,
    "Stack": settings.stage,
    "Owner": settings.owner,
    "Client": settings.client,
}.items():
    if value:
        Tags.of(lambda_stack).add(key, value)


app.synth()
