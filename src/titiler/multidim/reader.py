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
    import h5netcdf
except ImportError:  # pragma: nocover
    h5netcdf = None  # type: ignore

try:
    import zarr
except ImportError:  # pragma: nocover
    zarr = None  # type: ignore

api_settings = ApiSettings()
cache_client = get_redis()

settings = {
    "authorized_chunk_access": {
        "s3://nasa-waterinsight/NLDAS3/forcing/daily/": {"anonymous": True},
        "s3://podaac-ops-cumulus-protected/MUR-JPL-L4-GLOB-v4.1/": {"from_env": True},
    },
}


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

    print("DEBUG:SIMPLIFIED icechunk access without auth testing")
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
    # # I think it would be more elegant to get the virtual chunk containers and compare against authorized
    # # containers from settings but that might be slow?
    # config = icechunk.Repository.fetch_config(storage=storage)
    # vchunk_containers = config.virtual_chunk_containers.keys()
    # container_credentials = icechunk.containers_credentials(
    #     {k: icechunk.s3_credentials(from_env=True) for k in vchunk_containers}
    # )
    vchunk_creds = icechunk.containers_credentials(
        {
            prefix: icechunk.s3_credentials(**auth_kwargs)
            for prefix, auth_kwargs in authorize_virtual_chunk_access.items()
        }
    )

    repo = icechunk.Repository.open(
        storage=storage, authorize_virtual_chunk_access=vchunk_creds
    )
    print("DEBUG: opened icechunk repo")
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


def guess_opener(
    src_path: str,
    group: Optional[Any] = None,
    decode_times: bool = True,
    **kwargs: Any,
) -> xr.Dataset:
    """Guess the appropriate opener based on the file extension."""
    # for now simply try the icechunk opener and if it fails, fall back to xarray open_dataset.
    # In the future we may want to be more specific about which opener to use either based on a config or some other heuristic

    if os.path.isdir(src_path) and os.path.exists(os.path.join(src_path, "manifests")):
        return opener_icechunk(
            src_path,
            group=group,
            decode_times=decode_times,
            authorize_virtual_chunk_access=settings[
                "authorized_chunk_access"
            ],  # TODO this needs to be moved further up the stack so it can be passed in from the API call or some other config. For now we hardcode it here for testing purposes.
        )
    else:
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
    ) -> List[str]:
        """List available variable in a dataset."""
        # todo: why is this not a method of the reader class? Seems like a smell that I have to define the opener here again....
        with guess_opener(
            src_path,
            group=group,
            decode_times=decode_times,
        ) as ds:
            return list(ds.data_vars)  # type: ignore
