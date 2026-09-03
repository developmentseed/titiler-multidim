"""Typed configuration and icechunk credential construction for virtual chunk access.

icechunk is imported inside the functions that need it, never at module
level: settings.py imports this module's models, and settings.py must stay
importable in the CDK deployment environment (`uv sync --only-group
deployment`), which does not install icechunk.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Iterable, Mapping, Optional, Union
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, model_validator

if TYPE_CHECKING:
    import icechunk


def _add_trailing(prefix: str) -> str:
    """Normalize like icechunk's add_trailing: append '/' only if absent."""
    return prefix if prefix.endswith("/") else prefix + "/"


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
            if name not in ("anonymous", "from_env", "earthdata")
            and getattr(self, name) is not None
        }
        if (
            getattr(self, "anonymous", None)
            or self.from_env
            or getattr(self, "earthdata", None)
            or static_fields
        ):
            return self
        raise ValueError(
            "credential use is opt-in: set 'from_env': true, 'anonymous': true "
            "(s3/gcs only), 'earthdata': true (s3 only), or explicit credential "
            "fields"
        )


class S3ChunkAccess(_CloudChunkAccess):
    """Options for an s3:// virtual chunk entry (icechunk.s3_credentials).

    ``earthdata: true`` instead fetches EDL-derived refreshable credentials
    for the entry's bucket via earthaccess-auth's CMR bucket registry
    (requires the bucket to be registered there and an EDL identity:
    EARTHDATA_TOKEN or netrc).
    """

    anonymous: bool | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    earthdata: bool | None = None

    @model_validator(mode="after")
    def _earthdata_is_exclusive(self) -> "S3ChunkAccess":
        if self.earthdata:
            others = {
                name
                for name in self.model_fields_set
                if name != "earthdata" and getattr(self, name) is not None
            }
            if others:
                raise ValueError(
                    "'earthdata': true cannot be combined with other access "
                    f"options (got: {', '.join(sorted(others))})"
                )
        return self

    def to_credential(self, prefix: str) -> icechunk.AnyS3Credential:
        """Build the icechunk credential from the explicitly-set fields.

        Pure local construction, no network I/O: EDL identity and
        credential priming happen in the opener, and only for containers
        the opened repository actually declares (see earthdata_endpoints).

        Args:
            prefix: The entry's container URL prefix; earthdata entries
                resolve it to a DAAC via the CMR bucket registry.
        """
        if self.earthdata:
            # import lazy: earthaccess-auth is absent in the CDK deployment
            # environment; earthdata_s3_credentials resolves the prefix via
            # the CMR bucket registry itself and raises the typed
            # S3CredentialsEndpointUnresolved for an unregistered bucket
            # (parse_chunk_access already rejects those at parse time)
            from earthaccess_auth.adapters.icechunk import (
                earthdata_s3_credentials,
            )

            return earthdata_s3_credentials(prefix)

        import icechunk

        return icechunk.s3_credentials(
            **self.model_dump(exclude_unset=True, exclude={"earthdata"})
        )


class GcsChunkAccess(_CloudChunkAccess):
    """Options for a gs://gcs:// virtual chunk entry (icechunk.gcs_credentials)."""

    anonymous: bool | None = None
    service_account_file: str | None = None
    service_account_key: str | None = None
    application_credentials: str | None = None
    bearer_token: str | None = None

    def to_credential(self) -> icechunk.AnyGcsCredential:
        """Build the icechunk credential from the explicitly-set fields."""
        import icechunk

        return icechunk.gcs_credentials(**self.model_dump(exclude_unset=True))


class AzureChunkAccess(_CloudChunkAccess):
    """Options for an az://azure:// virtual chunk entry (icechunk.azure_credentials).

    icechunk has no anonymous Azure credential variant, so unlike s3/gcs there
    is no anonymous field here.
    """

    access_key: str | None = None
    sas_token: str | None = None
    bearer_token: str | None = None

    def to_credential(self) -> icechunk.AnyAzureCredential:
        """Build the icechunk credential from the explicitly-set fields."""
        import icechunk

        return icechunk.azure_credentials(**self.model_dump(exclude_unset=True))


AnyChunkAccess = Union[S3ChunkAccess, GcsChunkAccess, AzureChunkAccess]

# raw option dicts (from a caller) or parsed models (from settings)
ChunkAccessMapping = Mapping[str, Union[Mapping[str, Any], AnyChunkAccess]]

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
        # prefixes byte for byte apart from appending a missing trailing
        # slash, so an entry spelled 'S3://…' would validate here yet
        # never match at request time
        if not prefix.startswith(f"{scheme}://"):
            raise ValueError(
                f"virtual chunk entry {prefix!r} must begin with lowercase "
                f"'{scheme}://'; icechunk matches container prefixes by exact "
                "string (apart from a trailing slash), so other spellings are "
                "silently ignored"
            )
        if isinstance(options, BaseModel):
            if not isinstance(options, model):
                raise ValueError(
                    f"virtual chunk entry {prefix!r} expects {model.__name__}, "
                    f"got {type(options).__name__}"
                )
            entry = options
        else:
            try:
                entry = model.model_validate(options)
            except ValueError as e:
                raise ValueError(
                    f"invalid options for virtual chunk entry {prefix!r}: {e}"
                ) from e

        if isinstance(entry, S3ChunkAccess) and entry.earthdata:
            # fail fast at parse time (README: "validated at application
            # startup") rather than per-request in to_credential; import
            # lazy, and only reached when an earthdata entry actually
            # exists, so this module stays importable without
            # earthaccess-auth (CDK deployment env) when no earthdata
            # entries are configured
            from earthaccess_auth.daac import resolve_bucket

            if resolve_bucket(prefix) is None:
                raise ValueError(
                    f"virtual chunk entry {prefix!r} sets 'earthdata': true "
                    "but its bucket is not in the CMR-derived bucket registry"
                )

        # store keys the way icechunk stores credential keys (add_trailing:
        # append-only, never collapsing), so matching stays byte-identical
        parsed[_add_trailing(prefix)] = entry
    return parsed


def earthdata_endpoints(
    entries: Mapping[str, AnyChunkAccess],
    declared_prefixes: Iterable[str],
) -> list[str]:
    """Collect the ``s3credentials`` endpoints for earthdata entries a repo declares.

    Only entries whose prefix matches a virtual chunk container the opened
    repository actually declares are resolved, so an earthdata entry for
    another repo's bucket never couples this open to Earthdata Login
    availability or EULA state.

    Args:
        entries: Already-parsed entries (see parse_chunk_access), so
            callers that also build credentials parse the config once.
        declared_prefixes: URL prefixes of the virtual chunk containers
            the opened repository declares.

    Returns:
        Sorted ``s3credentials`` endpoint URLs.
    """
    # icechunk appends a missing trailing slash to container prefixes and
    # credential keys (append-only — a doubled slash is preserved and
    # simply never matches); entries were normalized the same way at parse
    # time, so a plain membership test reproduces icechunk's matching
    declared = {_add_trailing(p) for p in declared_prefixes}
    endpoints = set()
    for prefix, entry in entries.items():
        if (
            isinstance(entry, S3ChunkAccess)
            and entry.earthdata
            and prefix in declared
        ):
            from earthaccess_auth.daac import resolve_bucket

            info = resolve_bucket(prefix)
            if info is not None:  # parse_chunk_access guarantees registration
                endpoints.add(info.endpoint)
    return sorted(endpoints)


def build_virtual_chunk_access(
    entries: Mapping[str, AnyChunkAccess],
) -> Optional[Dict[str, Optional[icechunk.AnyCredential]]]:
    """Translate parsed virtual chunk access entries into icechunk credentials.

    Each entry builds its own icechunk credential via to_credential().

    Args:
        entries: Already-parsed entries (see parse_chunk_access, which
            rejects file://, unknown schemes, and unrecognized options),
            so callers parse the config exactly once.

    Returns:
        The mapping for Repository.open(authorize_virtual_chunk_access), or
        None when no entries are configured.
    """
    import icechunk

    if not entries:
        return None
    return icechunk.containers_credentials(
        {
            # only s3 entries resolve their prefix (earthdata routing)
            prefix: (
                options.to_credential(prefix)
                if isinstance(options, S3ChunkAccess)
                else options.to_credential()
            )
            for prefix, options in entries.items()
        }
    )
