"""XarrayReader"""

import pickle
import os
from typing import Any, List, Optional, Dict
from urllib.parse import urlparse


import attr
import xarray as xr

from titiler.multidim.redis_pool import get_redis
from titiler.multidim.settings import ApiSettings
from titiler.xarray.io import Reader, get_variable, xarray_open_dataset

try:
    import icechunk
except ImportError:  # pragma: nocover
    icechunk = None  # type: ignore

try:
    import fsspec
except ImportError:  # pragma: nocover
    fsspec = None  # type: ignore

try:
    import obstore
except ImportError:  # pragma: nocover
    obstore = None  # type: ignore

try:
    import h5netcdf
except ImportError:  # pragma: nocover
    h5netcdf = None  # type: ignore

try:
    import zarr
except ImportError:  # pragma: nocover
    zarr = None  # type: ignore

api_settings = ApiSettings()
cache_client = get_redis()


def opener_icechunk(
    src_path: str,
    group: Optional[Any] = None,
    decode_times: bool = True,
    authorize_virtual_chunk_access: Optional[Dict[str, Dict[str, Any]]] = None,
) -> xr.Dataset:
    """Open an IceChunk dataset using xarray."""
    assert icechunk is not None, "'icechunk' must be installed to read icechunk dataset"

    # TODO: This will likely be repeated across openers. Can we somehow handle this in the Reader Class?
    parsed = urlparse(src_path)
    protocol = parsed.scheme or "file"

    authorize_virtual_chunk_access = authorize_virtual_chunk_access or {}

    if protocol == "file":
        storage = icechunk.local_filesystem_storage(src_path)
    elif protocol == "s3":
        storage = icechunk.s3_storage(
            bucket=parsed.netloc,
            prefix=parsed.path.lstrip(
                "/"
            ),  # remove leading slash, this is an annoying mismatch between icechunk and urlparse
            from_env=True,  # we always assume that we can get credentials from env vars or IAM role for the store itself?
        )
    else:
        raise NotImplementedError(
            f"icechunk storage for protocol {protocol} is not implemented"
        )
    # TODO: I think it would be more elegant to get the virtual chunk containers and
    # compare against authorized containers from settings but that might be slowing things down. Leaving this for later.

    vchunk_creds = (
        icechunk.containers_credentials(
            {
                prefix: icechunk.s3_credentials(**auth_kwargs)
                for prefix, auth_kwargs in authorize_virtual_chunk_access.items()
            }
        )
        if authorize_virtual_chunk_access
        else None
    )

    repo = icechunk.Repository.open(
        storage=storage, authorize_virtual_chunk_access=vchunk_creds
    )
    session = repo.readonly_session("main")
    store = session.store
    return xr.open_dataset(
        store,
        group=group,
        decode_times=decode_times,
        engine="zarr",
        consolidated=False,
        zarr_format=3,
    )


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
        )
    else:
        raise NotImplementedError(
            f"Storage backend identification for protocol {protocol} is not implemented"
        )

    is_dir = _is_dir(store)
    if not is_dir:
        # assume this is a file, and detect the format based on the file extension
        _, ext = os.path.splitext(parsed.path)
        if ext in [".nc", ".nc4"]:
            format = "h5netcdf"
        else:
            raise NotImplementedError(
                f"File format identification for extension {ext} is not implemented"
            )
    else:
        has_manifests = _is_dir(store, "manifests")
        if has_manifests:
            format = "icechunk"
        else:
            format = "zarr"
    return format


def guess_opener(
    src_path: str,
    group: Optional[Any] = None,
    decode_times: bool = True,
    authorize_virtual_chunk_access: Optional[Dict[str, Dict[str, Any]]] = None,
    **kwargs: Any,
) -> xr.Dataset:
    """Guess the storage backend and return an xarray Dataset.

    Args:
        src_path: Path to the dataset
        group: Optional group/subgroup to open
        decode_times: Whether to decode time coordinates
        authorize_virtual_chunk_access: Authorization config for icechunk virtual chunks
        **kwargs: Additional arguments to pass to the opener

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
    else:
        # For zarr, h5netcdf, or other formats, use the standard xarray opener
        return xarray_open_dataset(
            src_path, group=group, decode_times=decode_times, **kwargs
        )


@attr.s
class XarrayReader(Reader):
    """Custom XarrayReader with redis cache"""

    def __attrs_post_init__(self):
        """Set bounds and CRS."""
        self.opener = guess_opener
        ds = None
        # Generate cache key and attempt to fetch the dataset from cache
        cache_key = f"{self.src_path}_group:{self.group}_time:{self.decode_times}"

        if api_settings.enable_cache:
            data_bytes = cache_client.get(cache_key)
            if data_bytes:
                print(f"Found dataset in Cache {cache_key}")
                ds = pickle.loads(data_bytes)

        # If opener_options doesn't have authorize_virtual_chunk_access, use settings
        if "authorize_virtual_chunk_access" not in self.opener_options:
            self.opener_options["authorize_virtual_chunk_access"] = (
                api_settings.authorized_chunk_access
            )

        self.ds = ds or self.opener(
            self.src_path,
            group=self.group,
            decode_times=self.decode_times,
            **self.opener_options,
        )

        if not ds and api_settings.enable_cache:
            # Serialize the dataset to bytes using pickle
            cache_key = f"{self.src_path}_group:{self.group}_time:{self.decode_times}"
            data_bytes = pickle.dumps(self.ds)
            print(f"Adding dataset in Cache: {cache_key}")
            cache_client.set(cache_key, data_bytes, ex=300)

        self.input = get_variable(
            self.ds,
            self.variable,
            sel=self.sel,
        )
        super().__attrs_post_init__()

    @classmethod
    def list_variables(
        cls,
        src_path: str,
        group: Optional[Any] = None,
        decode_times: bool = True,
        opener_options: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """List available variable in a dataset."""
        # todo: why is this not a method of the reader class? Seems like a smell that I have to define the opener here again....
        opener_options = opener_options or {}
        # If opener_options doesn't have authorize_virtual_chunk_access, use settings
        if "authorize_virtual_chunk_access" not in opener_options:
            opener_options["authorize_virtual_chunk_access"] = (
                api_settings.authorized_chunk_access
            )

        with guess_opener(
            src_path,
            group=group,
            decode_times=decode_times,
            **opener_options,
        ) as ds:
            return list(ds.data_vars)  # type: ignore
