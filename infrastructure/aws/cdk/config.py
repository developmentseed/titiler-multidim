"""STACK Configs."""

from getpass import getuser
from typing import Annotated, Dict, List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class StackSettings(BaseSettings):
    """CDK Stack settings"""

    titiler_multidim_stack_name: str = "titiler-multidim"
    stage: str = Field(
        ...,
        description=(
            "Deployment stage used to name stack and resources, "
            "i.e. `dev`, `staging`, `prod`"
        ),
    )

    owner: str = Field(
        description=" ".join(
            [
                "Name of primary contact for Cloudformation Stack.",
                "Used to tag generated resources",
                "Defaults to current username.",
            ]
        ),
        default_factory=getuser,
    )

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

    def cdk_env(self) -> dict:
        """Load a cdk environment dict for stack"""

        if self.vpc_id:
            return {
                "account": self.cdk_default_account,
                "region": self.cdk_default_region,
            }
        else:
            return {}

    class Config:
        """model config"""

        env_file = ".env"
        extra = "ignore"


class AppSettings(BaseSettings):
    """Application settings"""

    reader_role_arn: Annotated[
        str, "arn for IAM role with priveleges required for reading data"
    ]
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
        extra = "ignore"
        env_prefix = "TITILER_MULTIDIM_"
