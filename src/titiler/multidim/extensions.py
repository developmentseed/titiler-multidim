"""Local dataset opener for TiTiler's metadata extensions."""

from typing import Any

import xarray as xr

from titiler.multidim.reader import api_settings, guess_opener


def open_metadata_dataset(src_paths: list[str], **kwargs: Any) -> xr.Dataset:
    """Open the first dataset URL with configured Icechunk access."""
    return guess_opener(
        src_paths[0],
        authorize_virtual_chunk_access=api_settings.authorized_chunk_access,
        **kwargs,
    )
