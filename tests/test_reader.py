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


ENDPOINT = "https://archive.podaac.earthdata.nasa.gov/s3credentials"
REGISTRY_PREFIX = "s3://podaac-ops-cumulus-protected/MUR/"


def test_cache_hit_primes_endpoints_recorded_in_dataset(monkeypatch):
    """A dataset unpickled from the shared cache carries a refreshable
    credential callable that needs an EDL identity in THIS process; the
    hit path must prime the endpoints the dataset recorded at open time
    (a fresh Lambda env with a shared Redis otherwise fails with an
    opaque wrapped LoginStrategyUnavailable)."""
    primed = []
    monkeypatch.setattr(
        "titiler.multidim.earthdata.prime_earthdata_endpoints",
        lambda endpoints: primed.append(list(endpoints)),
    )
    monkeypatch.setattr(reader, "cache_client", FakeRedis())
    monkeypatch.setattr(reader.api_settings, "enable_cache", True)

    def opener(*args, **kwargs):
        ds = _tiny_dataset()
        ds.encoding["earthdata_endpoints"] = [ENDPOINT]
        return ds

    monkeypatch.setattr(reader, "guess_opener", opener)
    reader.XarrayReader("cache.zarr", "data")
    assert primed == []  # miss path: the opener itself primes
    reader.XarrayReader("cache.zarr", "data")  # cache hit
    assert primed == [[ENDPOINT]]


def test_cache_hit_without_earthdata_endpoints_never_primes(monkeypatch):
    """Datasets that recorded no earthdata endpoints (plain zarr/NetCDF,
    icechunk without earthdata containers) must never touch the earthdata
    machinery — even when the service config has earthdata entries — so a
    Secrets Manager or EDL outage cannot fail unrelated requests."""
    primed = []
    monkeypatch.setattr(
        "titiler.multidim.earthdata.prime_earthdata_endpoints",
        lambda endpoints: primed.append(list(endpoints)),
    )
    monkeypatch.setattr(reader, "cache_client", FakeRedis())
    monkeypatch.setattr(reader.api_settings, "enable_cache", True)
    monkeypatch.setattr(reader, "guess_opener", lambda *a, **k: _tiny_dataset())
    access = {REGISTRY_PREFIX: {"earthdata": True}}
    for _ in range(2):  # miss, then hit
        reader.XarrayReader(
            "cache.zarr",
            "data",
            opener_options={"authorize_virtual_chunk_access": access},
        )
    assert not primed


class _StubRepo:
    def __init__(self, url_prefixes):
        class _Container:
            def __init__(self, url_prefix):
                self.url_prefix = url_prefix

        class _Config:
            virtual_chunk_containers = {
                f"c{i}": _Container(p) for i, p in enumerate(url_prefixes)
            }

        self.config = _Config()

    def readonly_session(self, branch):
        class _Session:
            store = object()

        return _Session()


def test_opener_icechunk_primes_only_declared_earthdata_containers(monkeypatch):
    """The opener primes EDL credentials only for earthdata containers the
    repo actually declares, records them on the dataset for the cache-hit
    path, and never touches EDL for a repo without earthdata containers."""
    primed = []
    monkeypatch.setattr(
        "titiler.multidim.earthdata.prime_earthdata_endpoints",
        lambda endpoints: primed.append(list(endpoints)),
    )
    monkeypatch.setattr(
        icechunk.Repository,
        "open",
        staticmethod(lambda **kwargs: _StubRepo([REGISTRY_PREFIX])),
    )
    monkeypatch.setattr(reader.xr, "open_dataset", lambda store, **k: _tiny_dataset())
    access = {
        REGISTRY_PREFIX: {"earthdata": True},
        "s3://nasa-waterinsight/NLDAS3/": {"anonymous": True},
    }
    ds = reader.opener_icechunk(
        "file:///tmp/repo", authorize_virtual_chunk_access=access
    )
    assert primed == [[ENDPOINT]]
    assert ds.encoding["earthdata_endpoints"] == [ENDPOINT]


def test_opener_icechunk_skips_earthdata_for_undeclared_containers(monkeypatch):
    """An earthdata entry for another repo's bucket must not couple this
    open to Earthdata availability or EULA state (a fully public dataset
    served alongside a protected one must never 403/500 on EDL problems)."""
    primed = []
    monkeypatch.setattr(
        "titiler.multidim.earthdata.prime_earthdata_endpoints",
        lambda endpoints: primed.append(list(endpoints)),
    )
    monkeypatch.setattr(
        icechunk.Repository,
        "open",
        staticmethod(lambda **kwargs: _StubRepo(["s3://nasa-waterinsight/NLDAS3/"])),
    )
    monkeypatch.setattr(reader.xr, "open_dataset", lambda store, **k: _tiny_dataset())
    access = {
        REGISTRY_PREFIX: {"earthdata": True},
        "s3://nasa-waterinsight/NLDAS3/": {"anonymous": True},
    }
    ds = reader.opener_icechunk(
        "file:///tmp/repo", authorize_virtual_chunk_access=access
    )
    assert primed == []
    assert "earthdata_endpoints" not in ds.encoding
