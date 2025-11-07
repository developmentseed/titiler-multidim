"""Titiler-xarray API settings."""

import json
from typing import Any, Dict

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    """FASTAPI application settings."""

    name: str = "titiler-multidim"
    cors_origins: str = "*"
    cors_allow_methods: str = "GET"
    cachecontrol: str = "public, max-age=3600"
    root_path: str = ""
    debug: bool = False

    model_config = SettingsConfigDict(env_prefix="TITILER_MULTIDIM_", env_file=".env")
    cache_host: str = "127.0.0.1"
    enable_cache: bool = True

    # Configuration for authorizing virtual chunk access in icechunk datasets
    # Format: {"s3://bucket/prefix/": {"anonymous": true}, "s3://other/": {"from_env": true}}
    authorized_chunk_access: Dict[str, Dict[str, Any]] = {}

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
        """Parse authorized_chunk_access from JSON string or dict."""
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in authorized_chunk_access: {e}") from e
        return v or {}
