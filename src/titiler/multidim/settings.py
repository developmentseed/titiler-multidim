"""Titiler-multidim API and deployment settings."""

import json
from getpass import getuser
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from titiler.multidim.chunk_access import AnyChunkAccess, parse_chunk_access


class ApiSettings(BaseSettings):
    """FastAPI application settings."""

    name: str = "titiler-multidim"
    cors_origins: str = "*"
    cors_allow_methods: str = "GET"
    cachecontrol: str = "public, max-age=3600"
    root_path: str = ""
    debug: bool = False
    telemetry_enabled: bool = False
    authorized_chunk_access: dict[str, AnyChunkAccess] = {}

    model_config = SettingsConfigDict(
        env_prefix="TITILER_MULTIDIM_", env_file=".env", extra="ignore"
    )

    @field_validator("cors_origins")
    def parse_cors_origin(cls, v):
        """Parse CORS origins."""
        return [origin.strip() for origin in v.split(",")]

    @field_validator("cors_allow_methods")
    def parse_cors_allow_methods(cls, v):
        """Parse CORS allowed methods."""
        return [method.strip().upper() for method in v.split(",")]

    @field_validator("authorized_chunk_access", mode="before")
    def parse_authorized_chunk_access(cls, v):
        """Parse authorized_chunk_access from JSON string or dict into models."""
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in authorized_chunk_access: {e}") from e
        return parse_chunk_access(v)


class StackSettings(BaseSettings):
    """CDK stack settings."""

    titiler_multidim_stack_name: str = "titiler-multidim"
    stage: str = Field(..., description="Deployment stage, e.g. dev, staging, prod")
    owner: str = Field(default_factory=getuser)
    veda_custom_host: str | None = Field(
        None, description="Custom host URL override for API Gateway integration"
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


class AppSettings(BaseSettings):
    """Lambda and application deployment settings."""

    reader_role_arn: str
    additional_env: dict = {}
    key: str = "*"
    timeout: int = 30
    memory: int = 3009
    telemetry_enabled: bool = False
    max_concurrent: int | None = None
    alarm_email: str | None = ""
    root_path: str = Field("", description="Optional root path for all API endpoints")
    authorized_chunk_access: str | None = Field(
        None,
        description="JSON string for authorizing virtual chunk access in icechunk datasets",
    )

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_prefix="TITILER_MULTIDIM_"
    )

    def model_post_init(self, __context: Any) -> None:
        """Add authorized_chunk_access to additional_env if set."""
        if self.authorized_chunk_access:
            self.additional_env["TITILER_MULTIDIM_AUTHORIZED_CHUNK_ACCESS"] = (
                self.authorized_chunk_access
            )
