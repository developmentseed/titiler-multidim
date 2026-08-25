"""Reader tests."""

import icechunk
import numpy as np
import obstore
import pytest
import xarray as xr
from fakeredis import FakeRedis

from titiler.multidim import reader


def test_reader_reuses_cached_dataset(monkeypatch):
    """Reuse a cached dataset instead of reopening it in the parent reader."""
    import titiler.multidim.reader as reader

    opened = 0

    def opener(*args, **kwargs):
        nonlocal opened
        opened += 1
        return xr.Dataset(
            {"data": (("y", "x"), np.ones((2, 2)))},
            coords={"x": [0, 1], "y": [1, 0]},
        ).rio.write_crs("EPSG:4326")

    monkeypatch.setattr(reader, "cache_client", FakeRedis())
    monkeypatch.setattr(reader.api_settings, "enable_cache", True)
    monkeypatch.setattr(reader, "guess_opener", opener)

    reader.XarrayReader("cache.zarr", "data")
    reader.XarrayReader("cache.zarr", "data")

    assert opened == 1


class _StopWiring(Exception):
    """Raised by stubs to stop execution once the wiring under test ran."""


def test_identify_uses_boto3_provider(monkeypatch):
    """identify_storage_backend always uses the ambient Boto3 provider for s3."""
    from obstore.auth.boto3 import Boto3CredentialProvider

    # Boto3CredentialProvider's constructor calls session.get_credentials()
    # synchronously and raises if it's None; this sandbox has no ambient AWS
    # credentials (no env vars, no ~/.aws, no IAM role), so without these the
    # test would fail on that construction before ever reaching the stubbed
    # S3Store. Setting them locally satisfies botocore's env credential
    # provider with no network call, matching this file's "stub every
    # EDL/S3 interaction" convention for the parts of the SDK we don't own.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    captured = {}

    def fake_s3store(**kwargs):
        captured.update(kwargs)
        raise _StopWiring

    monkeypatch.setattr(obstore.store, "S3Store", fake_s3store)
    with pytest.raises(_StopWiring):
        reader.identify_storage_backend("s3://some-random-bucket/f.nc")
    assert isinstance(captured["credential_provider"], Boto3CredentialProvider)


def test_opener_icechunk_uses_from_env_storage(monkeypatch):
    """opener_icechunk opens s3 stores with from_env (ambient) credentials."""
    captured = {}

    def fake_s3_storage(**kwargs):
        captured.update(kwargs)
        raise _StopWiring

    monkeypatch.setattr(icechunk, "s3_storage", fake_s3_storage)
    with pytest.raises(_StopWiring):
        reader.opener_icechunk("s3://some-random-bucket/repo")
    assert captured["from_env"] is True


def _tiny_dataset():
    return xr.Dataset(
        {"data": (("y", "x"), np.ones((2, 2)))},
        coords={"x": [0, 1], "y": [1, 0]},
    ).rio.write_crs("EPSG:4326")


def test_cache_key_varies_with_chunk_access_config(monkeypatch):
    """Changing the authorization config must not serve datasets cached
    under the old config (e.g. entries removed to revoke access)."""
    opened = 0

    def opener(*args, **kwargs):
        nonlocal opened
        opened += 1
        return _tiny_dataset()

    monkeypatch.setattr(reader, "cache_client", FakeRedis())
    monkeypatch.setattr(reader.api_settings, "enable_cache", True)
    monkeypatch.setattr(reader, "guess_opener", opener)

    access_a = {"s3://bucket-a/prefix/": {"anonymous": True}}
    access_b = {"s3://bucket-b/prefix/": {"from_env": True}}
    reader.XarrayReader(
        "cache.zarr",
        "data",
        opener_options={"authorize_virtual_chunk_access": access_a},
    )
    reader.XarrayReader(
        "cache.zarr",
        "data",
        opener_options={"authorize_virtual_chunk_access": access_b},
    )
    assert opened == 2  # different config -> different key -> reopened

    reader.XarrayReader(
        "cache.zarr",
        "data",
        opener_options={"authorize_virtual_chunk_access": access_a},
    )
    assert opened == 2  # same config -> cache hit


def test_cache_hit_with_earthdata_entry_ensures_identity(monkeypatch):
    """A dataset unpickled from the shared cache carries a refreshable
    credential callable that needs an EDL identity in THIS process; the
    hit path must establish it (a fresh Lambda env with a shared Redis
    otherwise fails with an opaque wrapped LoginStrategyUnavailable)."""
    ensured = []
    monkeypatch.setattr(
        "titiler.multidim.earthdata.ensure_earthdata_credentials",
        lambda: ensured.append(True),
    )
    monkeypatch.setattr(reader, "cache_client", FakeRedis())
    monkeypatch.setattr(reader.api_settings, "enable_cache", True)
    monkeypatch.setattr(reader, "guess_opener", lambda *a, **k: _tiny_dataset())

    access = {"s3://asdc-prod-protected/": {"earthdata": True}}
    reader.XarrayReader(
        "cache.zarr",
        "data",
        opener_options={"authorize_virtual_chunk_access": access},
    )
    ensured.clear()
    reader.XarrayReader(  # cache hit
        "cache.zarr",
        "data",
        opener_options={"authorize_virtual_chunk_access": access},
    )
    assert ensured  # identity established on the hit path too


def test_cache_hit_without_earthdata_entries_skips_ensure(monkeypatch):
    """A cache hit for a config with no earthdata entries never calls ensure."""
    ensured = []
    monkeypatch.setattr(
        "titiler.multidim.earthdata.ensure_earthdata_credentials",
        lambda: ensured.append(True),
    )
    monkeypatch.setattr(reader, "cache_client", FakeRedis())
    monkeypatch.setattr(reader.api_settings, "enable_cache", True)
    monkeypatch.setattr(reader, "guess_opener", lambda *a, **k: _tiny_dataset())
    reader.XarrayReader("cache.zarr", "data")
    reader.XarrayReader("cache.zarr", "data")
    assert not ensured
