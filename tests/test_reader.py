"""Reader tests."""

import icechunk
import numpy as np
import obstore
import pytest
import xarray as xr

from titiler.multidim import reader


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


ENDPOINT = "https://archive.podaac.earthdata.nasa.gov/s3credentials"
REGISTRY_PREFIX = "s3://podaac-ops-cumulus-protected/MUR/"


class _StubRepo:
    def __init__(self, url_prefixes):
        class _Container:
            store = "s3"

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
    repo actually declares, and never touches EDL for a repo without
    earthdata containers."""
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
    reader.opener_icechunk("file:///tmp/repo", authorize_virtual_chunk_access=access)
    assert primed == [[ENDPOINT]]


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
    reader.opener_icechunk("file:///tmp/repo", authorize_virtual_chunk_access=access)
    assert primed == []


class TestApplyWhere:
    """Behavior of the `where` masking at the reader level."""

    @pytest.fixture
    def store(self, tmp_path_factory):
        """A zarr store with 3D data, a 2D mask, and a 1D variable."""
        rng = np.random.default_rng(42)
        lat = np.linspace(-85.0, 85.0, 18)
        lon = np.linspace(-175.0, 175.0, 36)
        flag = np.zeros((18, 36))
        flag[0, 0] = np.nan
        flag[0, 1] = 1.0
        ds = xr.Dataset(
            {
                "data": (("time", "lat", "lon"), rng.random((4, 18, 36))),
                "mask2d": (("lat", "lon"), rng.random((18, 36))),
                "line": (("time",), np.arange(4.0)),
                # same grid shape, offset by 0.1 deg: get_variable renames
                # latitude/longitude to y/x too, so only coordinate values
                # distinguish it from the data's grid
                "offgrid": (("latitude", "longitude"), rng.random((18, 36))),
                # 0 = good, 1 = bad, NaN at [0, 0] = no retrieval (fill)
                "flag": (("lat", "lon"), flag),
            },
            coords={
                "time": np.arange(4),
                "lat": lat,
                "lon": lon,
                "latitude": lat + 0.1,
                "longitude": lon + 0.1,
            },
        )
        path = str(tmp_path_factory.mktemp("where") / "store.zarr")
        ds.chunk({"time": 1, "lat": 9, "lon": 9}).to_zarr(path, consolidated=False)
        return path

    def _reader(self, store, **kwargs):
        return reader.XarrayReader(
            src_path=store,
            variable="data",
            decode_times=False,
            sel=["time=0"],
            **kwargs,
        )

    def test_non_spatial_condition_variable_is_a_400(self, store):
        """A 0/1-D condition variable must raise BadRequestError, not ValueError."""
        from titiler.core.errors import BadRequestError

        with pytest.raises(BadRequestError, match="line"):
            self._reader(store, where=["line>0"])

    def test_mask_without_selector_dims_is_accepted(self, store):
        """A (lat, lon) mask must work even when the request selects on time."""
        with self._reader(store, where=["mask2d>=0"]) as src:
            assert src.point(0, 0).array[0] is not np.ma.masked

    def test_dataset_closed_when_where_is_invalid(self, store, monkeypatch):
        """A 400 raised by _apply_where must not leak the opened dataset."""
        from titiler.core.errors import BadRequestError

        closed = []
        real_opener = reader.guess_opener

        def spy_opener(*args, **kwargs):
            ds = real_opener(*args, **kwargs)
            real_close = ds._close
            ds.set_close(lambda: (closed.append(True), real_close and real_close()))
            return ds

        monkeypatch.setattr(reader, "guess_opener", spy_opener)
        with pytest.raises(BadRequestError):
            self._reader(store, where=["data=1"])
        assert closed == [True]

    def test_where_masking_stays_lazy(self, store):
        """Masking must not materialize the full slice at reader construction."""
        with self._reader(store, where=["mask2d>=0.5"]) as src:
            assert not src.input._in_memory

    def test_mask_on_mismatched_grid_is_a_400(self, store):
        """A mask whose coordinates differ from the data's must 400 —
        .where() would align with join='inner' and silently shrink or
        empty the data while bounds/transform go stale."""
        from titiler.core.errors import BadRequestError

        with pytest.raises(BadRequestError, match="offgrid"):
            self._reader(store, where=["offgrid>=0"])

    def test_where_preserves_encoding(self, store):
        """.where() returns a bare array; losing encoding would turn
        rio.nodata (from encoding['_FillValue']) into None whenever a
        where= filter is present."""
        def encodings_equal(enc1, enc2):
            """Compare encodings, treating NaN values as equal."""
            if enc1.keys() != enc2.keys():
                return False
            for key in enc1.keys():
                v1, v2 = enc1[key], enc2[key]
                # Handle NaN values specially (NaN != NaN in Python)
                if isinstance(v1, float) and isinstance(v2, float):
                    if np.isnan(v1) and np.isnan(v2):
                        continue
                if v1 != v2:
                    return False
            return True

        with (
            self._reader(store) as plain,
            self._reader(store, where=["mask2d>=0"]) as masked,
        ):
            assert plain.input.encoding  # fixture must actually carry encoding
            assert encodings_equal(masked.input.encoding, plain.input.encoding)

    def test_fill_in_condition_variable_fails_the_filter(self, store):
        """NaN != 1 is True, so without a notnull guard a no-retrieval
        pixel passes `flag!=1` while failing the equivalent `flag==0`."""
        with self._reader(store, where=["flag!=1"]) as src:
            # [0, 0] is lat=-85, lon=-175, where flag is NaN (fill)
            assert src.point(-175.0, -85.0).array[0] is np.ma.masked

    def test_contiguous_netcdf_gets_bounded_chunks(self, tmp_path):
        """A contiguous variable has no preferred_chunks; the fallback must
        not become one whole-variable dask chunk (dask 'auto' would)."""
        rng = np.random.default_rng(7)
        ds = xr.Dataset(
            {
                "data": (("lat", "lon"), rng.random((2000, 8))),
                "mask2d": (("lat", "lon"), np.ones((2000, 8))),
            },
            coords={
                "lat": np.linspace(-85.0, 85.0, 2000),
                "lon": np.linspace(-175.0, 175.0, 8),
            },
        )
        path = str(tmp_path / "contiguous.nc")
        ds.to_netcdf(path, engine="h5netcdf")
        with reader.XarrayReader(
            src_path=path, variable="data", decode_times=False, where=["mask2d>=0"]
        ) as src:
            assert max(src.input.chunksizes["y"]) <= reader._FALLBACK_CHUNK
