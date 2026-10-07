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
                # strings, which no numeric condition can compare against
                "label": (("lat", "lon"), np.full((18, 36), "ok")),
                # same grid shape, offset by 0.1 deg: get_variable renames
                # latitude/longitude to y/x too, so only coordinate values
                # distinguish it from the data's grid
                "offgrid": (("latitude", "longitude"), rng.random((18, 36))),
                # 0 = good, 1 = bad, NaN at [0, 0] = no retrieval (fill)
                "flag": (("lat", "lon"), flag),
                # has a depth dim not in the data, so it cannot mask it
                "deep": (("depth", "lat", "lon"), np.zeros((2, 18, 36))),
            },
            coords={
                "time": np.arange(4),
                "lat": lat,
                "lon": lon,
                "latitude": lat + 0.1,
                "longitude": lon + 0.1,
                "depth": [0, 1],
            },
        )
        path = str(tmp_path_factory.mktemp("where") / "store.zarr")
        # on-disk chunks via encoding (ds.chunk() would need dask)
        chunks = {
            "data": (1, 9, 9),
            "mask2d": (9, 9),
            "offgrid": (9, 9),
            "flag": (9, 9),
            "line": (1,),
        }
        ds.to_zarr(
            path,
            consolidated=False,
            encoding={name: {"chunks": c} for name, c in chunks.items()},
        )
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
        """A 0/1-D condition variable must raise WhereConditionError, not ValueError."""
        with pytest.raises(reader.WhereConditionError, match="line"):
            self._reader(store, where=["line>0"])

    def test_non_numeric_condition_variable_is_a_400(self, store):
        """A non-numeric condition variable must fail at construction, not on
        the first read."""

        with pytest.raises(reader.WhereConditionError, match="not numeric"):
            self._reader(store, where=["label==1"])

    def test_mask_without_selector_dims_is_accepted(self, store):
        """A (lat, lon) mask must work even when the request selects on time."""
        with self._reader(store, where=["mask2d>=0"]) as src:
            assert src.point(0, 0).array[0] is not np.ma.masked

    def test_dataset_closed_when_where_is_invalid(self, store, monkeypatch):
        """A 400 raised by _apply_where must not leak the opened dataset."""
        closed = []
        real_opener = reader.guess_opener

        def spy_opener(*args, **kwargs):
            ds = real_opener(*args, **kwargs)
            real_close = ds._close
            ds.set_close(lambda: (closed.append(True), real_close and real_close()))
            return ds

        monkeypatch.setattr(reader, "guess_opener", spy_opener)
        with pytest.raises(reader.WhereConditionError):
            self._reader(store, where=["nope==1"])
        assert closed == [True]

    def test_invalid_syntax_is_a_400_before_the_store_is_opened(self, tmp_path):
        """A malformed condition must 400 without any I/O. The store does not
        exist, so opening it before parsing would raise a different error."""

        with pytest.raises(reader.WhereConditionError, match="expected"):
            self._reader(str(tmp_path / "missing.zarr"), where=["data=1"])

    def test_where_masking_stays_lazy(self, store):
        """Masking must not materialize the full slice at reader construction."""
        with self._reader(store, where=["mask2d>=0.5"]) as src:
            assert not src.input._in_memory

    def test_mask_on_mismatched_grid_is_a_400(self, store):
        """A mask whose coordinates differ from the data's must 400, because
        masks are applied by pixel position rather than by coordinates."""
        with pytest.raises(reader.WhereConditionError, match="offgrid"):
            self._reader(store, where=["offgrid>=0"])

    def test_where_preserves_encoding(self, store):
        """Masking keeps the variable's encoding, without which `rio.nodata`
        (from `encoding['_FillValue']`) would be `None` whenever a `where=`
        filter is present."""

        with (
            self._reader(store) as plain,
            self._reader(store, where=["mask2d>=0"]) as masked,
        ):
            assert plain.input.encoding  # fixture must actually carry encoding
            np.testing.assert_equal(masked.input.encoding, plain.input.encoding)

    def test_fill_in_condition_variable_fails_the_filter(self, store):
        """NaN != 1 is True, so without a notnull guard a no-retrieval
        pixel passes `flag!=1` while failing the equivalent `flag==0`."""
        with self._reader(store, where=["flag!=1"]) as src:
            # [0, 0] is lat=-85, lon=-175, where flag is NaN (fill)
            assert src.point(-175.0, -85.0).array[0] is np.ma.masked

    def test_part_reads_only_the_window(self, store, monkeypatch):
        """Masking must stay inside xarray's lazy-indexing layer: a part()
        read materializes the clipped window of the data and the mask, not
        the full slice (18x36 here)."""
        shapes = []
        read_window = reader._MaskedArray._read_window

        def spy(self, key):
            out = read_window(self, key)
            shapes.append(out.shape)
            return out

        monkeypatch.setattr(reader._MaskedArray, "_read_window", spy)
        with self._reader(store, where=["mask2d>=0.5"]) as src:
            assert not src.input._in_memory
            img = src.part((-30.0, -30.0, 30.0, 30.0), width=8, height=8)
        assert img.array.shape == (1, 8, 8)
        assert shapes and all(h < 18 and w < 36 for h, w in shapes), shapes

    def test_mask_applies_before_reprojection(self, store):
        """With bilinear resampling, masked pixels must be NaN before the
        warp (excluded from the kernel), matching an eager .where() of the
        same slice: masking after the warp would bleed masked neighbours."""
        bbox = (-100.0, -50.0, 100.0, 50.0)
        kwargs = {"width": 40, "height": 20, "reproject_method": "bilinear"}
        with self._reader(store, where=["mask2d>=0.5", "flag==0"]) as src:
            img = src.part(bbox, **kwargs)
            ds = src.ds
            eager = src.input.copy(data=src.input.values)  # forces the lazy mask
        assert img.array.dtype == np.float64  # data is float64, no upcast
        reference = (
            ds["data"]
            .isel(time=0)
            .where((ds["mask2d"] >= 0.5) & (ds["flag"] == 0) & ds["flag"].notnull())
        )
        np.testing.assert_array_equal(eager.values, reference.values)
        from rio_tiler.io import XarrayReader as RioXarrayReader

        # `eager` keeps the input's encoding (nodata), which the warp uses to
        # exclude masked source pixels from the bilinear kernel
        with RioXarrayReader(input=eager) as ref:
            expected = ref.part(bbox, **kwargs)
        np.testing.assert_array_equal(img.array.mask, expected.array.mask)
        np.testing.assert_allclose(img.array.filled(0), expected.array.filled(0))

    def test_mask_with_dims_not_in_the_data_is_a_400(self, store):
        """A mask with a dimension not in the data must fail at construction,
        not on the first read."""

        with pytest.raises(reader.WhereConditionError, match="depth"):
            self._reader(store, where=["deep>0"])

    def test_conditions_on_the_selected_and_a_repeated_variable(self, store):
        """A condition on the selected variable tests the data itself, and
        every condition on a repeated variable applies."""

        where = ["data>=0.25", "mask2d>=0.25", "mask2d<0.75"]

        with self._reader(store, where=where) as src:
            masked = src.input.values
            data = src.ds["data"].isel(time=0).values
            mask2d = src.ds["mask2d"].values

        keep = (data >= 0.25) & (mask2d >= 0.25) & (mask2d < 0.75)

        np.testing.assert_array_equal(masked, np.where(keep, data, np.nan))
