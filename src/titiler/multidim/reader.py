"""XarrayReader"""

from __future__ import annotations

import logging
import operator
import os
import re
import time
from typing import (
    Any,
    Dict,
    List,
    Optional,
)
from urllib.parse import urlparse

import attr
import icechunk
import obstore
import xarray as xr
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
    # For zarr, h5netcdf, or other formats, use the standard xarray opener
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


_WHERE_CONDITION = re.compile(
    r"^\s*(?P<variable>[\w.-]+)\s*(?P<op>==|!=|<=|>=|<|>)\s*"
    r"(?P<value>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*$"
)

_WHERE_OPS: Dict[str, Any] = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


@attr.s
class XarrayReader(Reader):
    """Custom XarrayReader with Icechunk and virtual chunk support."""

    where: Optional[List[str]] = attr.ib(default=None, kw_only=True)

    def __attrs_post_init__(self):
        """Configure the custom opener before the parent reads the dataset."""
        self.opener_options = _inject_settings(self.opener_options)
        self.opener = guess_opener
        log_path = _log_path(self.src_path)
        logger.info("Initializing Xarray reader spatial metadata: source=%s", log_path)
        started_at = time.monotonic()
        super().__attrs_post_init__()
        self._apply_where()
        logger.info(
            "Initialized Xarray reader spatial metadata: source=%s elapsed_seconds=%.2f",
            log_path,
            time.monotonic() - started_at,
        )

    def _apply_where(self) -> None:
        """Mask the selected variable by the `where` conditions.

        Each condition is `{variable}{op}{number}` against another variable
        of the same dataset, extracted with the same `sel` selectors so the
        mask and the data describe the same slice. Conditions are ANDed;
        failing pixels become NaN and follow the normal nodata path.
        """
        if not self.where:
            return
        mask = None
        for condition in self.where:
            parsed = _WHERE_CONDITION.match(condition)
            if not parsed:
                raise BadRequestError(
                    f"Invalid where condition {condition!r}: expected "
                    "`{variable}{op}{number}` with op one of "
                    f"{', '.join(_WHERE_OPS)}"
                )
            name = parsed["variable"]
            if name not in self.ds:
                raise BadRequestError(
                    f"Invalid where condition {condition!r}: variable "
                    f"{name!r} not found in the dataset"
                )
            try:
                da = get_variable(self.ds, name, sel=self.sel)
            except (KeyError, AssertionError) as e:
                raise BadRequestError(
                    f"Invalid where condition {condition!r}: {name!r} does "
                    "not accept this request's `sel` selectors"
                ) from e
            extra_dims = set(da.dims) - set(self.input.dims)
            if extra_dims:
                raise BadRequestError(
                    f"Invalid where condition {condition!r}: {name!r} has "
                    f"dimensions {sorted(map(str, extra_dims))} that "
                    f"{self.variable!r} does not"
                )
            comparison = _WHERE_OPS[parsed["op"]](da, float(parsed["value"]))
            mask = comparison if mask is None else mask & comparison
        self.input = self.input.where(mask)

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
