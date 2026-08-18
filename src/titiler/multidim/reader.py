"""XarrayReader"""

from __future__ import annotations

import os
import pickle
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Union,
)
from urllib.parse import urlparse

import attr
import icechunk
import obstore
import xarray as xr
from boto3.session import Session
from obstore.auth.boto3 import Boto3CredentialProvider
from titiler.xarray.io import Reader, xarray_open_dataset

from titiler.multidim.redis_pool import get_redis
from titiler.multidim.settings import (
    AnyChunkAccess,
    ApiSettings,
    AzureChunkAccess,
    GcsChunkAccess,
    S3ChunkAccess,
    parse_chunk_access,
)

# raw option dicts (from a caller) or parsed models (from settings)
ChunkAccessMapping = Mapping[str, Union[Mapping[str, Any], AnyChunkAccess]]

api_settings = ApiSettings()
cache_client = get_redis()


if TYPE_CHECKING:
    # the input union of icechunk.containers_credentials, which exports no
    # alias for it (icechunk.AnyCredential is its narrower OUTPUT union)
    _ContainerCredential = Union[
        icechunk.AnyS3Credential,
        icechunk.AnyGcsCredential,
        icechunk.AnyAzureCredential,
    ]


def build_virtual_chunk_access(
    authorize_virtual_chunk_access: Optional[ChunkAccessMapping],
) -> Optional[Dict[str, Optional[icechunk.AnyCredential]]]:
    """Translate virtual chunk access settings into icechunk credentials.

    Entries are parsed into per-scheme option models by parse_chunk_access
    (which also rejects file://, unknown schemes, and unrecognized options),
    then the set fields of each entry are passed to the matching icechunk
    credential builder (e.g. icechunk.s3_credentials).

    Args:
        authorize_virtual_chunk_access: Mapping of container URL prefix to
            access options (raw dicts or parsed models).

    Returns:
        The mapping for Repository.open(authorize_virtual_chunk_access), or
        None when no entries are configured.
    """
    entries = parse_chunk_access(authorize_virtual_chunk_access)
    if not entries:
        return None

    credential_builders: Dict[type, Callable[..., _ContainerCredential]] = {
        S3ChunkAccess: icechunk.s3_credentials,
        GcsChunkAccess: icechunk.gcs_credentials,
        AzureChunkAccess: icechunk.azure_credentials,
    }
    credentials: Dict[str, _ContainerCredential] = {}
    for prefix, options in entries.items():
        builder = credential_builders[type(options)]
        credentials[prefix] = builder(**options.model_dump(exclude_unset=True))
    return icechunk.containers_credentials(credentials)


def opener_icechunk(
    src_path: str,
    group: Optional[str] = None,
    decode_times: bool = True,
    authorize_virtual_chunk_access: Optional[ChunkAccessMapping] = None,
) -> xr.Dataset:
    """Open an IceChunk dataset using xarray."""
    credentials = build_virtual_chunk_access(authorize_virtual_chunk_access)

    # TODO: For future opener development. This will likely be repeated across openers. Can we somehow handle this in the Reader Class?
    parsed = urlparse(src_path)
    protocol = parsed.scheme or "file"

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

    repo = icechunk.Repository.open(
        storage=storage, authorize_virtual_chunk_access=credentials
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
        session = Session()
        credential_provider = Boto3CredentialProvider(session)
        store = obstore.store.S3Store(
            bucket=parsed.netloc,
            prefix=parsed.path.lstrip("/"),
            credential_provider=credential_provider,
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
        cache_key = (
            f"{src_path}_group:{kwargs.get('group')}_time:{kwargs.get('decode_times')}"
        )

        if api_settings.enable_cache:
            data_bytes = cache_client.get(cache_key)
            if data_bytes:
                return pickle.loads(data_bytes)

        ds = guess_opener(src_path, **kwargs)

        if api_settings.enable_cache:
            cache_client.set(cache_key, pickle.dumps(ds), ex=300)

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
