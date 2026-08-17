"""Titiler-multidim API and deployment settings."""

import json
from getpass import getuser
from typing import Annotated, Any, Dict, Mapping, Union
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class _CloudChunkAccess(BaseModel):
    """Options shared by the cloud (s3/gcs/azure) virtual chunk entries.

    Each field mirrors the JSON-expressible keyword of the matching icechunk
    credential builder; only fields the user set are forwarded to it.
    Credential use is opt-in: an entry must select an access mode explicitly
    (anonymous where the builder supports it, from_env, or explicit credential
    fields), so an empty entry cannot silently grant the service's own ambient
    credentials.
    """

    model_config = ConfigDict(extra="forbid")

    from_env: bool | None = None

    @model_validator(mode="after")
    def _require_explicit_access_mode(self) -> "_CloudChunkAccess":
        # icechunk's builders fall back to FromEnv (the service's own ambient
        # credentials) when called with no arguments; requiring an explicit
        # mode keeps that a deliberate grant instead of a default
        static_fields = {
            name
            for name in self.model_fields_set
            if name not in ("anonymous", "from_env") and getattr(self, name) is not None
        }
        if getattr(self, "anonymous", None) or self.from_env or static_fields:
            return self
        raise ValueError(
            "credential use is opt-in: set 'from_env': true, 'anonymous': true "
            "(s3/gcs only), or explicit credential fields"
        )


class S3ChunkAccess(_CloudChunkAccess):
    """Options for an s3:// virtual chunk entry (icechunk.s3_credentials)."""

    anonymous: bool | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None


class GcsChunkAccess(_CloudChunkAccess):
    """Options for a gs://gcs:// virtual chunk entry (icechunk.gcs_credentials)."""

    anonymous: bool | None = None
    service_account_file: str | None = None
    service_account_key: str | None = None
    application_credentials: str | None = None
    bearer_token: str | None = None


class AzureChunkAccess(_CloudChunkAccess):
    """Options for an az://azure:// virtual chunk entry (icechunk.azure_credentials).

    icechunk has no anonymous Azure credential variant, so unlike s3/gcs there
    is no anonymous field here.
    """

    access_key: str | None = None
    sas_token: str | None = None
    bearer_token: str | None = None


AnyChunkAccess = Union[S3ChunkAccess, GcsChunkAccess, AzureChunkAccess]

_SCHEME_MODELS: Dict[str, type[AnyChunkAccess]] = {
    "s3": S3ChunkAccess,
    "gs": GcsChunkAccess,
    "gcs": GcsChunkAccess,
    "az": AzureChunkAccess,
    "azure": AzureChunkAccess,
}


def parse_chunk_access(
    mapping: Mapping[str, Union[Mapping[str, Any], AnyChunkAccess]] | None,
) -> Dict[str, AnyChunkAccess]:
    """Parse a virtual chunk access config into per-scheme option models.

    Each entry is dispatched by the URL scheme of its container prefix.
    Already-parsed models pass through unchanged when they match their
    prefix's scheme; a mismatched model, "file://" (must never read the
    server's local filesystem), and unknown schemes are rejected.
    """
    parsed: Dict[str, AnyChunkAccess] = {}
    for prefix, options in (mapping or {}).items():
        scheme = urlparse(prefix).scheme
        model = _SCHEME_MODELS.get(scheme)
        if model is None:
            if scheme == "file":
                raise ValueError(
                    f"refusing to authorize local filesystem virtual chunk access for {prefix!r}; "
                    "virtual chunks must not read the server's local filesystem"
                )
            raise ValueError(
                f"unsupported scheme {scheme!r} for virtual chunk entry {prefix!r}"
            )
        # urlparse lowercases the scheme, but icechunk matches container
        # prefixes character for character, so an entry spelled 'S3://…'
        # would validate here yet never match at request time
        if not prefix.startswith(f"{scheme}://"):
            raise ValueError(
                f"virtual chunk entry {prefix!r} must begin with lowercase "
                f"'{scheme}://'; icechunk matches container prefixes by exact "
                "string, so other spellings are silently ignored"
            )
        if isinstance(options, BaseModel):
            if not isinstance(options, model):
                raise ValueError(
                    f"virtual chunk entry {prefix!r} expects {model.__name__}, "
                    f"got {type(options).__name__}"
                )
            parsed[prefix] = options
            continue
        try:
            parsed[prefix] = model.model_validate(options)
        except ValueError as e:
            raise ValueError(
                f"invalid options for virtual chunk entry {prefix!r}: {e}"
            ) from e
    return parsed


class ApiSettings(BaseSettings):
    """FastAPI application settings."""

    name: str = "titiler-multidim"
    cors_origins: str = "*"
    cors_allow_methods: str = "GET"
    cachecontrol: str = "public, max-age=3600"
    root_path: str = ""
    debug: bool = False
    telemetry_enabled: bool = False
    cache_host: str = "127.0.0.1"
    enable_cache: bool = True
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
    vpc_id: Annotated[str | None, "VPC id; creates a new one if not provided"] = None
    cdk_default_account: str | None = Field(
        None, description="AWS account id required when deploying to an existing VPC"
    )
    cdk_default_region: str | None = Field(
        None, description="AWS region required when deploying to an existing VPC"
    )
    veda_custom_host: str | None = Field(
        None, description="Custom host URL override for API Gateway integration"
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def cdk_env(self) -> dict:
        """Return CDK environment dict for stack."""
        if self.vpc_id:
            return {
                "account": self.cdk_default_account,
                "region": self.cdk_default_region,
            }
        return {}


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
