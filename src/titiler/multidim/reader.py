"""XarrayReader"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
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
from pydantic import BaseModel
from titiler.xarray.io import Reader, xarray_open_dataset

from titiler.multidim.chunk_access import (
    ChunkAccessMapping,
    build_virtual_chunk_access,
    earthdata_endpoints,
    parse_chunk_access,
)
from titiler.multidim.redis_pool import get_redis
from titiler.multidim.settings import ApiSettings

api_settings = ApiSettings()
cache_client = get_redis()


def opener_icechunk(
    src_path: str,
    group: Optional[str] = None,
    decode_times: bool = True,
    authorize_virtual_chunk_access: Optional[ChunkAccessMapping] = None,
) -> xr.Dataset:
    """Open an IceChunk dataset using xarray."""
    # parse once; build_virtual_chunk_access and earthdata_endpoints both
    # consume the parsed models
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

    repo = icechunk.Repository.open(
        storage=storage, authorize_virtual_chunk_access=credentials
    )
    containers = repo.config.virtual_chunk_containers or {}
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
    ds = xr.open_dataset(
        store,
        group=group,
        decode_times=decode_times,
        engine="zarr",
        consolidated=False,
        zarr_format=3,
    )
    if endpoints:
        # encoding survives pickling but is never served in API responses,
        # so the cache-hit path can re-prime exactly these endpoints in a
        # process that didn't open the dataset itself
        ds.encoding["earthdata_endpoints"] = endpoints
    return ds


# TODO Is there a better way to check if a url points to a file or a prefix?
def _is_dir(store, path: str = "") -> bool:
    """Return True if path is a prefix containing any objects (directory-like)."""
    # sanitize path and slashes
    path = path.rstrip("/") + "/"
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
    storage_format = identify_storage_backend(src_path)

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


def _entry_options(entry: Any) -> Dict[str, Any]:
    """Normalize a chunk-access entry (raw dict or parsed model) to a dict."""
    if isinstance(entry, BaseModel):
        return entry.model_dump(exclude_unset=True)
    return dict(entry)


def _access_fingerprint(access: Optional[ChunkAccessMapping]) -> str:
    """Stable digest of the authorization config for the dataset cache key.

    Datasets are cached with their credentials/authorization baked in
    (icechunk credential callables, s3fs options), so a config change must
    miss the cache instead of serving data authorized under the old config.
    """
    canonical = json.dumps(
        {prefix: _entry_options(entry) for prefix, entry in (access or {}).items()},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


@attr.s
class XarrayReader(Reader):
    """Custom XarrayReader with redis cache"""

    def __attrs_post_init__(self):
        """Configure the cached opener before the parent reads the dataset."""
        self.opener_options = _inject_settings(self.opener_options)
        self.opener = self._open_cached
        super().__attrs_post_init__()

    def _open_cached(self, src_path: str, **kwargs: Any) -> xr.Dataset:
        """Open a dataset, reusing its Redis cache entry when enabled."""
        access = kwargs.get("authorize_virtual_chunk_access")
        cache_key = (
            f"{src_path}_group:{kwargs.get('group')}"
            f"_time:{kwargs.get('decode_times')}"
            f"_access:{_access_fingerprint(access)}"
        )

        if api_settings.enable_cache:
            data_bytes = cache_client.get(cache_key)
            if data_bytes:
                endpoints, ds = pickle.loads(data_bytes)
                if endpoints:
                    # the unpickled dataset carries a refreshable credential
                    # callable that resolves through default_manager() in
                    # *this* process; the entry records which endpoints it
                    # needs, so re-prime exactly those (typed EULA/login
                    # errors surface here instead of opaquely in Rust)
                    from titiler.multidim.earthdata import (
                        prime_earthdata_endpoints,
                    )

                    prime_earthdata_endpoints(endpoints)
                return ds

        ds = guess_opener(src_path, **kwargs)

        if api_settings.enable_cache:
            # the cache entry is an explicit (endpoints, dataset) pair: the
            # cross-process re-prime contract lives in the cache layer, not
            # inside xarray metadata (the opener's encoding slot is only the
            # in-process handoff, popped here)
            endpoints = ds.encoding.pop("earthdata_endpoints", None)
            cache_client.set(cache_key, pickle.dumps((endpoints, ds)), ex=300)

        return ds

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
