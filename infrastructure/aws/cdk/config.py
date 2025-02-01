"""STACK Configs."""

from typing import Annotated, Dict, List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class StackSettings(BaseSettings):
    """Application settings"""

    name: str = "titiler-xarray"
    stage: str = "production"

    owner: Optional[str] = None
    client: Optional[str] = None
    project: Optional[str] = None

    reader_role_arn: Annotated[
        str, "arn for IAM role with priveleges required for reading data"
    ]
    vpc_id: Annotated[
        Optional[str],
        "VPC id to use for this stack, will create a new one if not provide",
    ] = None

    cdk_default_account: Optional[str] = Field(
        None,
        description="When deploying from a local machine the AWS account id is required to deploy to an existing VPC",
    )
    cdk_default_region: Optional[str] = Field(
        None,
        description="When deploying from a local machine the AWS region id is required to deploy to an existing VPC",
    )
    additional_env: Dict = {}

    # S3 bucket names where TiTiler could do HEAD and GET Requests
    # specific private and public buckets MUST be added if you want to use s3:// urls
    # You can whitelist all bucket by setting `*`.
    # ref: https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-arn-format.html
    buckets: List = []

    # S3 key pattern to limit the access to specific items (e.g: "my_data/*.tif")
    key: str = "*"

    timeout: int = 30
    memory: int = 3009

    # The maximum of concurrent executions you want to reserve for the function.
    # Default: - No specific limit - account limit.
    max_concurrent: Optional[int] = None
    alarm_email: Optional[str] = ""

    class Config:
        """model config"""

        env_file = ".env"
        env_prefix = "STACK_"
