"""XarrayReader"""

import pickle
from typing import Any, List, Optional

import attr

from titiler.multidim.redis_pool import get_redis
from titiler.multidim.settings import ApiSettings
from titiler.xarray.io import Reader, get_variable, xarray_open_dataset

api_settings = ApiSettings()
cache_client = get_redis()


@attr.s
class XarrayReader(Reader):
    """Custom XarrayReader with redis cache"""

    def __attrs_post_init__(self):
        """Set bounds and CRS."""
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
            datetime=self.datetime,
            drop_dim=self.drop_dim,
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
        with xarray_open_dataset(
            src_path,
            group=group,
            decode_times=decode_times,
        ) as ds:
            return list(ds.data_vars)  # type: ignore
