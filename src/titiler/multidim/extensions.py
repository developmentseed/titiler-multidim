"""TiTiler extensions adapted for multidimensional mosaics."""

from collections.abc import Callable
from typing import Annotated, Any

import xarray as xr
from attrs import define
from fastapi import Depends, Query
from starlette.responses import HTMLResponse
from titiler.core.factory import BaseFactory
from titiler.core.resources.enums import MediaType
from titiler.xarray.extensions import ValidateExtension, ValidationInfo

from titiler.multidim.reader import api_settings, guess_opener


def DatasetMetadataPathParams(
    url: str = Query(description="One Xarray dataset URL."),
) -> str:
    """Return the source URL accepted by dataset metadata endpoints."""
    return url


def open_metadata_dataset(src_path: str, **kwargs: Any) -> xr.Dataset:
    """Open a dataset for metadata inspection with configured Icechunk access."""
    return guess_opener(
        src_path,
        authorize_virtual_chunk_access=api_settings.authorized_chunk_access,
        **kwargs,
    )


@define
class DatasetMetadataExtension(ValidateExtension):
    """Register single-source dataset metadata and validation endpoints."""

    dataset_opener: Callable[..., xr.Dataset] = open_metadata_dataset

    def register(self, factory: BaseFactory) -> None:
        """Register dataset routes without changing the factory's source dependency."""

        @factory.router.get(
            "/dataset/",
            responses={
                200: {
                    "description": "Returns the HTML representation of the Xarray Dataset.",
                    "content": {MediaType.html.value: {}},
                },
            },
            response_class=HTMLResponse,
        )
        def dataset_metadata_html(
            src_path=Depends(DatasetMetadataPathParams),
            io_params=Depends(self.io_dependency),
        ):
            """Return the HTML representation of one Xarray Dataset."""
            with self.dataset_opener(src_path, **io_params.as_dict()) as ds:
                return HTMLResponse(ds._repr_html_())

        @factory.router.get(
            "/dataset/dict",
            responses={
                200: {"description": "Returns the full Xarray dataset as a dictionary."}
            },
        )
        def dataset_metadata_dict(
            src_path=Depends(DatasetMetadataPathParams),
            io_params=Depends(self.io_dependency),
        ):
            """Return one Xarray Dataset as a dictionary."""
            with self.dataset_opener(src_path, **io_params.as_dict()) as ds:
                return ds.to_dict(data=False)

        @factory.router.get(
            "/dataset/keys",
            response_model=list[str],
            responses={
                200: {
                    "description": "Returns the list of keys/variables in the Dataset."
                }
            },
        )
        def dataset_variables(
            src_path=Depends(DatasetMetadataPathParams),
            io_params=Depends(self.io_dependency),
        ):
            """Return the data variables in one Xarray Dataset."""
            with self.dataset_opener(src_path, **io_params.as_dict()) as ds:
                return list(ds.data_vars)

        @factory.router.get(
            "/validate",
            responses={200: {"content": {"application/json": {}}}},
            response_model=dict[str, ValidationInfo],
        )
        def validate_dataset(
            src_path=Depends(DatasetMetadataPathParams),
            io_params=Depends(self.io_dependency),
            variables: Annotated[
                list[str] | None, Query(description="Xarray Variable name.")
            ] = None,
        ):
            """Validate variables in one Xarray Dataset."""
            with self.dataset_opener(src_path, **io_params.as_dict()) as ds:
                variables = variables or list(ds.data_vars)
                return {
                    variable: self._validate_variable(ds[variable])
                    for variable in variables
                }
