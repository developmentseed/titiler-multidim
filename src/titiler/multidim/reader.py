"""XarrayReader"""

from __future__ import annotations

import logging
import operator
import os
import re
import time
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
)
from urllib.parse import urlparse

import attr
import icechunk
import numpy as np
import obstore
import xarray as xr
import zarr
from xarray.backends import BackendArray
from xarray.core import indexing
from boto3.session import Session
from obstore.auth.boto3 import Boto3CredentialProvider
from titiler.core.errors import BadRequestError
from titiler.xarray.io import Reader, get_variable, xarray_open_dataset

from titiler.multidim.chunk_access import (
    ChunkAccessMapping,
    build_virtual_chunk_access,
    earthdata_endpoints,
    parse_chunk_access,
)
from titiler.multidim.settings import ApiSettings

api_settings = ApiSettings()
logger = logging.getLogger(__name__)


def _log_path(src_path: str) -> str:
    """Return a source path without a potentially sensitive query string."""
    return src_path.split("?", maxsplit=1)[0]


def opener_icechunk(
    src_path: str,
    group: Optional[str] = None,
    decode_times: bool = True,
    authorize_virtual_chunk_access: Optional[ChunkAccessMapping] = None,
) -> xr.Dataset:
    """Open an IceChunk dataset using xarray."""
    # the config is parsed exactly once; build_virtual_chunk_access and
    # earthdata_endpoints both consume the parsed models
    entries = parse_chunk_access(authorize_virtual_chunk_access)
    credentials = build_virtual_chunk_access(entries)

    # TODO: For future opener development. This will likely be repeated across openers. Can we somehow handle this in the Reader Class?
    parsed = urlparse(src_path)
    protocol = parsed.scheme or "file"

    if protocol == "file":
        storage = icechunk.local_filesystem_storage(src_path)
    elif protocol == "s3":
        bucket = parsed.netloc
        prefix = parsed.path.lstrip(
            "/"
        )  # remove leading slash, this is an annoying mismatch between icechunk and urlparse
        storage = icechunk.s3_storage(
            bucket=bucket,
            prefix=prefix,
            from_env=True,  # the store itself always uses ambient credentials
        )
    else:
        raise NotImplementedError(
            f"icechunk storage for protocol {protocol} is not implemented"
        )

    log_path = _log_path(src_path)
    logger.info("Opening Icechunk repository: source=%s", log_path)
    started_at = time.monotonic()
    repo = icechunk.Repository.open(
        storage=storage, authorize_virtual_chunk_access=credentials
    )
    logger.info(
        "Opened Icechunk repository: source=%s elapsed_seconds=%.2f",
        log_path,
        time.monotonic() - started_at,
    )
    containers = repo.config.virtual_chunk_containers or {}
    for prefix, container in containers.items():
        logger.info(
            "Icechunk virtual chunk container: source=%s prefix=%s store=%s",
            log_path,
            prefix,
            container.store,
        )
    endpoints = earthdata_endpoints(
        entries,
        (container.url_prefix for container in containers.values()),
    )
    if endpoints:
        # establish the EDL identity and surface typed errors (EULA 403s)
        # in Python — only for containers this repo actually declares, so
        # a repo without earthdata containers never touches EDL
        from titiler.multidim.earthdata import prime_earthdata_endpoints

        prime_earthdata_endpoints(endpoints)
    session = repo.readonly_session("main")
    store = session.store
    logger.info("Opening Icechunk dataset: source=%s group=%s", log_path, group)
    started_at = time.monotonic()
    dataset = xr.open_dataset(
        store,  # type: ignore[arg-type]  # the zarr engine accepts stores; xarray's hints don't
        group=group,
        decode_times=decode_times,
        engine="zarr",
        consolidated=False,
        zarr_format=3,
    )
    logger.info(
        "Opened Icechunk dataset: source=%s elapsed_seconds=%.2f",
        log_path,
        time.monotonic() - started_at,
    )
    return dataset


def opener_zarr(
    src_path: str,
    group: Optional[str] = None,
    decode_times: bool = True,
    **kwargs: Any,
) -> xr.Dataset:
    """Open a Zarr store with xarray's lazy (unchunked) arrays.

    Mirrors titiler.xarray's fs_open_dataset zarr branch but pins
    chunks=None so every variable stays on xarray's lazy-indexing layer
    (a windowed read costs one store request per touched chunk, no task
    graph). `where=` masking builds on that same layer (see `_MaskedArray`),
    so nothing here needs a chunk manager.
    """
    store = zarr.storage.FsspecStore.from_url(
        src_path, storage_options={"asynchronous": True, **kwargs}
    )
    xr_open_args: Dict[str, Any] = {
        "decode_coords": "all",
        "decode_times": decode_times,
        "chunks": None,
    }
    if group is not None:
        xr_open_args["group"] = group
    return xr.open_zarr(store, **xr_open_args)


# TODO Is there a better way to check if a url points to a file or a prefix?
def _is_dir(store, path: str = "") -> bool:
    """Return True if path is a prefix containing any objects (directory-like)."""
    # sanitize path and slashes
    path = path.rstrip("/") + "/"
    logger.info("Checking dataset storage prefix: prefix=%s", path)
    stream = store.list(prefix=path, chunk_size=1)
    try:
        batch = next(stream)
        return len(batch) > 0
    except StopIteration:
        return False


def identify_storage_backend(src_path: str) -> str:
    """Identify the storage backend for a given path."""
    parsed = urlparse(src_path)
    protocol = parsed.scheme or "file"

    store: obstore.store.LocalStore | obstore.store.S3Store
    if protocol == "file":
        store = obstore.store.LocalStore(src_path)
    elif protocol == "s3":
        store = obstore.store.S3Store(
            bucket=parsed.netloc,
            prefix=parsed.path.lstrip("/"),
            credential_provider=Boto3CredentialProvider(Session()),
        )
    else:
        raise NotImplementedError(
            f"Storage backend identification for protocol {protocol} is not implemented"
        )

    if not _is_dir(store):
        # assume this is a file, and detect the format based on the file extension
        _, ext = os.path.splitext(parsed.path)
        if ext in [".nc", ".nc4"]:
            return "h5netcdf"
        raise NotImplementedError(
            f"File format identification for extension {ext} is not implemented"
        )
    if _is_dir(store, "manifests"):
        return "icechunk"
    return "zarr"


def guess_opener(
    src_path: str,
    group: Optional[str] = None,
    decode_times: bool = True,
    authorize_virtual_chunk_access: Optional[ChunkAccessMapping] = None,
    **kwargs: Any,
) -> xr.Dataset:
    """Guess the storage backend and return an xarray Dataset.

    Args:
        src_path: Path to the dataset
        group: Optional group/subgroup to open
        decode_times: Whether to decode time coordinates
        authorize_virtual_chunk_access: Authorization config for icechunk virtual chunks
        **kwargs: Additional arguments to pass to the opener.

    Returns:
        xarray.Dataset
    """

    # Identify the storage backend
    log_path = _log_path(src_path)
    logger.info("Identifying dataset storage: source=%s", log_path)
    started_at = time.monotonic()
    storage_format = identify_storage_backend(src_path)
    logger.info(
        "Identified dataset storage: source=%s format=%s elapsed_seconds=%.2f",
        log_path,
        storage_format,
        time.monotonic() - started_at,
    )

    if storage_format == "icechunk":
        return opener_icechunk(
            src_path,
            group=group,
            decode_times=decode_times,
            authorize_virtual_chunk_access=authorize_virtual_chunk_access,
        )
    if storage_format == "zarr":
        return opener_zarr(src_path, group=group, decode_times=decode_times, **kwargs)
    # For h5netcdf or other formats, use the standard xarray opener
    return xarray_open_dataset(
        src_path, group=group, decode_times=decode_times, **kwargs
    )


def _inject_settings(options: Dict[str, Any]) -> Dict[str, Any]:
    """Default the virtual chunk authorization to the service-wide setting.

    Returns a new dict, copying the settings value, so neither the caller's
    dict nor the process-wide authorization config is aliased into a reader.
    """
    options = dict(options)
    options.setdefault(
        "authorize_virtual_chunk_access", dict(api_settings.authorized_chunk_access)
    )
    return options


_WHERE_PREDICATE_BY_OP: Dict[str, Callable[[np.ndarray, float], np.ndarray]] = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}

# Use re.escape as a safety mechanism, in case an op string happens to contain
# any re metacharacter.
_WHERE_CONDITION_RE = re.compile(
    r"^\s*(?P<variable>[\w.-]+)\s*"
    rf"(?P<op>{'|'.join(map(re.escape, _WHERE_PREDICATE_BY_OP))})\s*"
    r"(?P<value>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*$"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class WhereCondition:
    """One parsed `{variable}{op}{number}` masking condition."""

    raw: str  # the original string, for error messages
    variable: str
    op: str
    value: float


def parse_where(conditions: Sequence[str]) -> List[WhereCondition]:
    """Parse `where=` condition strings. Syntax only: no dataset needed."""
    parsed = []
    invalid = []
    for condition in conditions:
        if match := _WHERE_CONDITION_RE.match(condition):
            parsed.append(
                WhereCondition(
                    raw=condition,
                    variable=match["variable"],
                    op=match["op"],
                    value=float(match["value"]),
                )
            )
        else:
            invalid.append(condition)
    if invalid:
        raise BadRequestError(
            f"Invalid where condition {', '.join(map(repr, invalid))}: expected "
            "`{variable}{op}{number}` with op one of "
            f"{', '.join(_WHERE_PREDICATE_BY_OP)}"
        )
    return parsed


class _MaskedArray(BackendArray):
    """Lazy `where=` mask: `data` with pixels failing every `masks` set to NaN.

    Xarray's lazy-indexing layer defers indexing only, so `data.where(mask)`
    on an unchunked variable would materialize the whole slice at reader
    construction. Wrapping this in `indexing.LazilyIndexedArray` instead
    keeps the mask inside that layer: rio-tiler's `clip_box` (an `isel`)
    stays lazy and the first materialization (`rio.reproject`,
    `to_masked_array`) reads only the requested window of `data` and of
    each mask variable, then combines them in numpy. Masking therefore
    still happens before warping/resampling, without dask.

    `masks` holds `(predicate, value, mask)` triples; a mask may lack some
    of `data`'s dimensions (a time-invariant flag under `sel=time=...`)
    and is indexed by the dimensions it has, then broadcast.
    """

    def __init__(
        self,
        data: xr.DataArray,
        masks: Sequence[
            tuple[Callable[[np.ndarray, float], np.ndarray], float, xr.DataArray]
        ],
    ) -> None:
        self.data = data
        self.masks = masks
        self.shape = data.shape
        # NaN needs a float dtype: same upcast xarray's .where() applied before
        self.dtype = np.result_type(data.dtype, np.float32)

    def __getitem__(self, key: indexing.ExplicitIndexer) -> np.ndarray:
        # OUTER: rioxarray's clip_box and titiler.xarray's sortby only need
        # slices and 1-D index arrays; vectorized keys get decomposed by xarray
        return indexing.explicit_indexing_adapter(
            key, self.shape, indexing.IndexingSupport.OUTER, self._read_window
        )

    def _read_window(self, key: tuple) -> np.ndarray:
        sel = dict(zip(self.data.dims, key))
        window = self.data.isel(sel).values
        keep = np.ones(window.shape, dtype=bool)
        for predicate, value, mask in self.masks:
            values = mask.isel({d: k for d, k in sel.items() if d in mask.dims}).values
            # NaN compares False for every operator except != — without
            # this a fill pixel in the flag variable passes `flag!=1`
            # while failing the equivalent `flag==0`
            keep &= predicate(values, value) & ~np.isnan(values)
        return np.where(keep, window, np.nan).astype(self.dtype, copy=False)


@attr.s
class XarrayReader(Reader):
    """Custom XarrayReader with Icechunk and virtual chunk support."""

    where: List[str] = attr.ib(factory=list, kw_only=True)

    def __attrs_post_init__(self):
        """Configure the custom opener before the parent reads the dataset."""
        self.opener_options = _inject_settings(self.opener_options)
        self.opener = guess_opener
        # parse before opening: a where= syntax error 400s without any I/O
        conditions = parse_where(self.where)
        log_path = _log_path(self.src_path)
        logger.info("Initializing Xarray reader spatial metadata: source=%s", log_path)
        started_at = time.monotonic()
        try:
            super().__attrs_post_init__()
            self._apply_where(conditions)
        except Exception:
            # super() can raise after opening (bad variable/sel, missing
            # spatial metadata), so close the dataset if it got that far
            if (ds := getattr(self, "ds", None)) is not None:
                ds.close()
            raise
        logger.info(
            "Initialized Xarray reader spatial metadata: source=%s elapsed_seconds=%.2f",
            log_path,
            time.monotonic() - started_at,
        )

    def _apply_where(self, conditions: Sequence[WhereCondition]) -> None:
        """Mask the selected variable by the `where` conditions.

        Each condition compares another variable of the same dataset,
        extracted with the request's `sel` selectors (restricted to the
        dimensions each mask variable has) so the mask and the data
        describe the same slice. Conditions are ANDed; failing pixels
        become NaN and follow the normal nodata path, so integer
        variables are upcast to float. Nothing is read here: the mask is
        applied lazily per window by `_MaskedArray`.
        """
        if not conditions:
            return
        if missing := sorted(
            {c.variable for c in conditions if c.variable not in self.ds}
        ):
            raise BadRequestError(
                f"Invalid where condition: variable {', '.join(map(repr, missing))} "
                "not found in the dataset"
            )

        # wrap self.input itself (the parent's one extraction) so bounds,
        # transform and pixels all come from the same DataArray
        ds = self.ds
        data = self.input
        masks = []
        for condition in conditions:
            name = condition.variable
            # a mask may legitimately lack some of the request's dimensions
            # (e.g. a time-invariant (y, x) mask under sel=time=...): apply
            # only the selectors whose dimension the mask variable has
            sel = [s for s in self.sel or [] if s.split("=", 1)[0] in ds[name].dims]
            try:
                da = get_variable(ds, name, sel=sel)
            except (KeyError, AssertionError, ValueError) as e:
                raise BadRequestError(
                    f"Invalid where condition {condition.raw!r}: {name!r} cannot "
                    f"mask {self.variable!r} for this request"
                ) from e
            extra_dims = set(da.dims) - set(self.input.dims)
            if extra_dims:
                raise BadRequestError(
                    f"Invalid where condition {condition.raw!r}: {name!r} has "
                    f"dimensions {sorted(map(str, extra_dims))} that "
                    f"{self.variable!r} does not"
                )
            # .where() aligns with join='inner': a mask on an offset or
            # coarser grid would silently shrink (or empty) the data while
            # bounds/transform, computed from the unmasked variable, go
            # stale — reject coordinate mismatches instead
            try:
                xr.align(data, da, join="exact")
            except ValueError as e:
                raise BadRequestError(
                    f"Invalid where condition {condition.raw!r}: {name!r} "
                    f"coordinates do not match {self.variable!r}'s"
                ) from e
            masks.append((_WHERE_PREDICATE_BY_OP[condition.op], condition.value, da))
        # copy(data=...) keeps coords, attrs and encoding (hence rio.nodata
        # and the CRS) and stays lazy: the parent already derived
        # bounds/transform from `data`, and this is the same array
        self.input = data.copy(
            data=indexing.LazilyIndexedArray(_MaskedArray(data, masks))
        )

    @classmethod
    def list_variables(
        cls,
        src_path: str,
        group: Optional[str] = None,
        decode_times: bool = True,
        opener_options: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """List available variable in a dataset."""
        opener_options = _inject_settings(opener_options or {})

        with guess_opener(
            src_path,
            group=group,
            decode_times=decode_times,
            **opener_options,
        ) as ds:
            return list(ds.data_vars)  # type: ignore
