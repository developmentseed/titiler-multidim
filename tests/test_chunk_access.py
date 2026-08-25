"""Unit tests for virtual chunk access handling in titiler.multidim.chunk_access."""

import inspect
import json
from datetime import datetime, timezone
from unittest import mock

import icechunk
import pytest
from pydantic import ValidationError

from titiler.multidim.chunk_access import (
    AzureChunkAccess,
    GcsChunkAccess,
    S3ChunkAccess,
    build_virtual_chunk_access,
    parse_chunk_access,
)
from titiler.multidim.settings import ApiSettings


def test_empty_mapping_returns_no_credentials():
    assert build_virtual_chunk_access({}) is None


def test_s3_entry_builds_credentials():
    credentials = build_virtual_chunk_access(
        {"s3://nasa-waterinsight/NLDAS3/": {"anonymous": True}}
    )
    assert credentials is not None
    assert "s3://nasa-waterinsight/NLDAS3/" in credentials


@pytest.mark.parametrize(
    "prefix",
    [
        "s3://bucket/prefix/",
        "gs://bucket/prefix/",
        "az://container/prefix/",
    ],
)
def test_empty_options_are_rejected(prefix):
    # credential use is opt-in for every scheme: an empty entry must fail
    # validation instead of silently granting the service's own credentials
    with pytest.raises(ValueError, match="opt-in"):
        build_virtual_chunk_access({prefix: {}})


def test_cloud_entry_with_no_access_mode_selected_is_rejected():
    # anonymous: false sets a field but selects no access mode, so icechunk
    # would still fall back to the service's ambient credentials
    with pytest.raises(ValueError, match="opt-in"):
        build_virtual_chunk_access({"s3://bucket/prefix/": {"anonymous": False}})


@pytest.mark.parametrize("prefix", ["gs://bucket/prefix/", "gcs://bucket/prefix/"])
def test_gcs_entry_builds_credentials(prefix):
    credentials = build_virtual_chunk_access({prefix: {"anonymous": True}})
    assert credentials is not None
    assert prefix in credentials


@pytest.mark.parametrize(
    "prefix", ["az://container/prefix/", "azure://container/prefix/"]
)
def test_azure_entry_builds_credentials(prefix):
    credentials = build_virtual_chunk_access({prefix: {"from_env": True}})
    assert credentials is not None
    assert prefix in credentials


def test_azure_anonymous_is_rejected():
    # icechunk has no anonymous Azure credential variant, so the option must
    # fail validation instead of raising TypeError when the dataset is opened
    with pytest.raises(ValueError, match="anonymous"):
        build_virtual_chunk_access({"az://container/prefix/": {"anonymous": True}})


def test_unknown_cloud_option_is_rejected():
    # an option outside the enumerated model fields must fail at parse time
    # instead of silently passing through to icechunk
    with pytest.raises(ValueError, match="region"):
        build_virtual_chunk_access(
            {
                "s3://bucket/prefix/": {
                    "access_key_id": "AKIA-TEST",
                    "region": "us-west-2",
                }
            }
        )


def test_mismatched_model_type_is_rejected():
    # a parsed model under a prefix of a different scheme must be rejected,
    # not silently converted when its set fields happen to overlap
    with pytest.raises(ValueError, match="expects S3ChunkAccess"):
        build_virtual_chunk_access(
            {"s3://bucket/prefix/": GcsChunkAccess(anonymous=True)}
        )


def test_settings_parse_returns_typed_models():
    settings = ApiSettings(
        authorized_chunk_access='{"s3://bucket/prefix/": {"anonymous": true}}'
    )
    entry = settings.authorized_chunk_access["s3://bucket/prefix/"]
    assert isinstance(entry, S3ChunkAccess)
    assert entry.anonymous is True


def test_settings_rejects_invalid_chunk_access_shape():
    with pytest.raises(ValueError, match="bogus_option"):
        ApiSettings(
            authorized_chunk_access='{"s3://bucket/prefix/": {"bogus_option": 1}}'
        )


def test_file_scheme_is_rejected():
    # virtual chunks must never read the server's local filesystem
    with pytest.raises(ValueError, match="local filesystem"):
        build_virtual_chunk_access({"file:///data/chunks/": {}})


def test_unsupported_scheme_raises():
    with pytest.raises(ValueError, match="ftp"):
        build_virtual_chunk_access({"ftp://host/prefix/": {"anonymous": True}})


def test_mixed_case_scheme_prefix_is_rejected():
    # urlparse lowercases the scheme for dispatch, but icechunk matches
    # container prefixes character for character, so an entry that validates
    # yet can never match must fail at parse time instead of silently doing
    # nothing at request time
    with pytest.raises(ValueError, match="lowercase"):
        build_virtual_chunk_access({"S3://bucket/prefix/": {"anonymous": True}})


@pytest.mark.parametrize(
    "model,builder",
    [
        (S3ChunkAccess, icechunk.s3_credentials),
        (GcsChunkAccess, icechunk.gcs_credentials),
        (AzureChunkAccess, icechunk.azure_credentials),
    ],
)
def test_model_fields_are_accepted_by_icechunk_builder(model, builder):
    # the chunk access models hand-mirror the JSON-expressible subset of the
    # icechunk credential builder signatures, so a rename or removal in a new
    # icechunk release must fail here instead of at request time.
    # 'earthdata' is titiler's own routing flag, never forwarded to icechunk
    # (to_credential excludes it from the model_dump it passes along), so it
    # is exempt from this parity check.
    accepted = set(inspect.signature(builder).parameters)
    missing = set(model.model_fields) - accepted - {"earthdata"}
    assert not missing, (
        f"{model.__name__} fields not accepted by {builder.__name__}: {missing}"
    )


def test_invalid_chunk_access_env_fails_settings_init(monkeypatch):
    monkeypatch.setenv(
        "TITILER_MULTIDIM_AUTHORIZED_CHUNK_ACCESS",
        json.dumps({"ftp://host/prefix/": {"anonymous": True}}),
    )
    with pytest.raises(ValidationError, match="ftp"):
        ApiSettings()


def test_settings_instantiated_at_import(monkeypatch):
    # the deploy-gate property behind fail-fast validation: ApiSettings() must
    # run at module import time, so an invalid config (rejected by the test
    # above) fails startup instead of the first request
    monkeypatch.setenv("TEST_ENVIRONMENT", "1")
    monkeypatch.delenv("TITILER_MULTIDIM_AUTHORIZED_CHUNK_ACCESS", raising=False)
    from titiler.multidim import main

    # the app fixture in conftest.py clears and re-imports titiler.multidim
    # modules, so the class imported at collection time may be a stale copy;
    # compare against the generation main actually imported
    from titiler.multidim.settings import ApiSettings as CurrentApiSettings

    assert isinstance(main.api_settings, CurrentApiSettings)


REGISTRY_PREFIX = "s3://podaac-ops-cumulus-protected/MUR/"
PODAAC_ENDPOINT = "https://archive.podaac.earthdata.nasa.gov/s3credentials"


def test_earthdata_satisfies_opt_in_validator():
    access = S3ChunkAccess(earthdata=True)
    assert access.earthdata is True


def test_earthdata_false_alone_is_rejected():
    with pytest.raises(ValueError, match="opt-in"):
        S3ChunkAccess(earthdata=False)


def test_earthdata_is_exclusive_with_other_modes():
    with pytest.raises(ValueError, match="cannot be combined"):
        S3ChunkAccess(earthdata=True, from_env=True)
    with pytest.raises(ValueError, match="cannot be combined"):
        S3ChunkAccess(earthdata=True, access_key_id="AKID")


def test_parse_chunk_access_accepts_earthdata_entry():
    parsed = parse_chunk_access({REGISTRY_PREFIX: {"earthdata": True}})
    assert isinstance(parsed[REGISTRY_PREFIX], S3ChunkAccess)
    assert parsed[REGISTRY_PREFIX].earthdata is True


def test_earthdata_to_credential_uses_registry_endpoint(monkeypatch):
    refreshable = mock.Mock(name="refreshable")
    factory = mock.Mock(return_value=refreshable)
    monkeypatch.setattr(
        "earthaccess_auth.adapters.icechunk.earthdata_s3_credentials", factory
    )
    monkeypatch.setattr(
        "earthaccess_auth.credentials.default_manager",
        lambda: _PrimingStubManager(),
    )
    access = S3ChunkAccess(earthdata=True)
    assert access.to_credential(REGISTRY_PREFIX) is refreshable
    factory.assert_called_once_with(PODAAC_ENDPOINT)


def test_parse_chunk_access_rejects_unregistered_earthdata_bucket():
    # the primary layer: an explicit 'earthdata': true entry for a bucket
    # outside the CMR-derived registry must fail fast at parse time (README:
    # "validated at application startup"), not per-request in to_credential
    with pytest.raises(
        ValueError, match="not-a-real-bucket.*not in the CMR-derived bucket registry"
    ):
        parse_chunk_access({"s3://not-a-real-bucket/prefix/": {"earthdata": True}})


def test_earthdata_to_credential_unknown_bucket_raises():
    # defensive backstop: to_credential re-checks the registry itself, so it
    # stays safe even when called directly (bypassing parse_chunk_access);
    # in normal service operation this is practically unreachable, since
    # parse_chunk_access above already rejects the entry first
    access = S3ChunkAccess(earthdata=True)
    with pytest.raises(ValueError, match="not in the CMR-derived bucket registry"):
        access.to_credential("s3://not-a-real-bucket/prefix/")


def _fake_earthdata_credentials() -> "icechunk.S3StaticCredentials":
    # module-level (not a test-local closure) so icechunk's real
    # s3_refreshable_credentials can pickle it, matching the picklable
    # functools.partial callable earthaccess_auth builds in production
    return icechunk.S3StaticCredentials(
        access_key_id="AKID",
        secret_access_key="SECRET",
        session_token="TOKEN",
        expires_after=datetime.now(timezone.utc),
    )


def test_earthdata_entry_builds_refreshable_credential(monkeypatch):
    monkeypatch.setattr(
        "earthaccess_auth.credentials.default_manager",
        lambda: _PrimingStubManager(),
    )
    with mock.patch(
        "earthaccess_auth.adapters.icechunk.get_credentials_callable",
        return_value=_fake_earthdata_credentials,
    ):
        creds = build_virtual_chunk_access({REGISTRY_PREFIX: {"earthdata": True}})
    assert creds is not None
    assert REGISTRY_PREFIX in creds


class _PrimingStubManager:
    def __init__(self):
        self.primed = []

    def get_credentials(self, endpoint):
        self.primed.append(endpoint)


def test_earthdata_to_credential_primes_endpoint_in_python(monkeypatch):
    """The first credential fetch must happen in Python, not inside
    icechunk's Rust callback: Rust re-wraps typed exceptions as opaque
    storage errors, losing the S3CredentialsRequestFailure (with EULA
    URLs) that main.py maps to HTTP 403. The manager caches per endpoint,
    so priming adds no extra EDL round trips."""
    manager = _PrimingStubManager()
    monkeypatch.setattr("earthaccess_auth.credentials.default_manager", lambda: manager)
    refreshable = mock.Mock(name="refreshable")
    monkeypatch.setattr(
        "earthaccess_auth.adapters.icechunk.earthdata_s3_credentials",
        mock.Mock(return_value=refreshable),
    )
    access = S3ChunkAccess(earthdata=True)
    assert access.to_credential(REGISTRY_PREFIX) is refreshable
    assert manager.primed == [PODAAC_ENDPOINT]


def test_earthdata_eula_rejection_raises_typed_from_build(monkeypatch):
    from earthaccess_auth.exceptions import S3CredentialsRequestFailure

    class _RaisingManager:
        def get_credentials(self, endpoint):
            raise S3CredentialsRequestFailure(
                "EULA not accepted: https://urs.earthdata.nasa.gov/approve"
            )

    monkeypatch.setattr(
        "earthaccess_auth.credentials.default_manager", lambda: _RaisingManager()
    )
    with pytest.raises(S3CredentialsRequestFailure, match="EULA"):
        build_virtual_chunk_access({REGISTRY_PREFIX: {"earthdata": True}})
