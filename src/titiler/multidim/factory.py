"""TiTiler.xarray factory."""

from typing import List, Literal, Optional, Type
from urllib.parse import urlencode

import jinja2
import numpy as np
from attrs import define
from fastapi import Depends, Query
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates
from typing_extensions import Annotated

from titiler.core.dependencies import DefaultDependency
from titiler.core.resources.enums import ImageType
from titiler.core.resources.responses import JSONResponse
from titiler.multidim.reader import XarrayReader
from titiler.xarray.dependencies import DatasetParams, XarrayIOParams, XarrayParams
from titiler.xarray.factory import TilerFactory as BaseTilerFactory


@define(kw_only=True)
class XarrayTilerFactory(BaseTilerFactory):
    """Xarray Tiler Factory."""

    reader: Type[XarrayReader] = XarrayReader
    reader_dependency: Type[DefaultDependency] = XarrayParams
    dataset_dependency: Type[DefaultDependency] = DatasetParams

    def register_routes(self) -> None:  # noqa: C901
        """Register Info / Tiles / TileJSON endoints."""
        super().register_routes()
        self.variables()

    def variables(self) -> None:
        """Register /variables endpoint"""

        @self.router.get(
            "/variables",
            response_class=JSONResponse,
            responses={200: {"description": "Return dataset's Variables."}},
        )
        def get_variables(
            src_path=Depends(self.path_dependency),
            io_params=Depends(XarrayIOParams),
        ) -> List[str]:
            """return available variables."""
            return self.reader.list_variables(
                src_path=src_path,
                group=io_params.group,
                decode_times=io_params.decode_times,
            )

    def statistics(self) -> None:
        """Register /statistics and /histogram endpoints"""
        super().statistics()

        @self.router.get(
            "/histogram",
            response_class=JSONResponse,
            responses={200: {"description": "Return histogram for this data variable"}},
            response_model_exclude_none=True,
        )
        def histogram(
            src_path=Depends(self.path_dependency),
            reader_params=Depends(self.reader_dependency),
        ):
            with self.reader(
                src_path=src_path,
                variable=reader_params.variable,
                group=reader_params.group,
                decode_times=reader_params.decode_times,
                datetime=reader_params.datetime,
            ) as src_dst:
                boolean_mask = ~np.isnan(src_dst.input)
                data_values = src_dst.input.values[boolean_mask]
                counts, values = np.histogram(data_values, bins=10)
                counts, values = counts.tolist(), values.tolist()
                buckets = list(
                    zip(values, [values[i + 1] for i in range(len(values) - 1)])
                )
                hist_dict = []
                for idx, bucket in enumerate(buckets):
                    hist_dict.append({"bucket": bucket, "value": counts[idx]})
                return hist_dict

    def map_viewer(self) -> None:
        """Register /map endpoints"""

        @self.router.get("/{tileMatrixSetId}/map", response_class=HTMLResponse)
        def map_viewer(
            request: Request,
            tileMatrixSetId: Annotated[  # type: ignore
                Literal[tuple(self.supported_tms.list())],
                "Identifier selecting one of the supported TileMatrixSetIds",
            ],
            url: Annotated[Optional[str], Query(description="Dataset URL")] = None,
            variable: Annotated[
                Optional[str],
                Query(description="Xarray Variable"),
            ] = None,
            group: Annotated[
                Optional[int],
                Query(
                    description="Select a specific zarr group from a zarr hierarchy, can be for pyramids or datasets. Can be used to open a dataset in HDF5 files."
                ),
            ] = None,
            decode_times: Annotated[
                bool,
                Query(
                    title="decode_times",
                    description="Whether to decode times",
                ),
            ] = True,
            drop_dim: Annotated[
                Optional[str],
                Query(description="Dimension to drop"),
            ] = None,
            datetime: Annotated[
                Optional[str], Query(description="Slice of time to read (if available)")
            ] = None,
            tile_format: Annotated[
                Optional[ImageType],
                Query(
                    description="Default will be automatically defined if the output image needs a mask (png) or not (jpeg).",
                ),
            ] = None,
            tile_scale: Annotated[
                int,
                Query(
                    gt=0, lt=4, description="Tile size scale. 1=256x256, 2=512x512..."
                ),
            ] = 1,
            minzoom: Annotated[
                Optional[int],
                Query(description="Overwrite default minzoom."),
            ] = None,
            maxzoom: Annotated[
                Optional[int],
                Query(description="Overwrite default maxzoom."),
            ] = None,
            post_process=Depends(self.process_dependency),
            colormap=Depends(self.colormap_dependency),
            render_params=Depends(self.render_dependency),
            dataset_params=Depends(self.dataset_dependency),
        ):
            """Return map Viewer."""
            titiler_templates = Jinja2Templates(
                env=jinja2.Environment(
                    loader=jinja2.ChoiceLoader([jinja2.PackageLoader("titiler.core")])
                )
            )
            local_templates = Jinja2Templates(
                env=jinja2.Environment(
                    loader=jinja2.ChoiceLoader([jinja2.PackageLoader(__package__, ".")])
                )
            )

            if url:
                tilejson_url = self.url_for(
                    request, "tilejson", tileMatrixSetId=tileMatrixSetId
                )
                if request.query_params._list:
                    tilejson_url += f"?{urlencode(request.query_params._list)}"

                tms = self.supported_tms.get(tileMatrixSetId)
                return titiler_templates.TemplateResponse(
                    name="map.html",
                    context={
                        "request": request,
                        "tilejson_endpoint": tilejson_url,
                        "tms": tms,
                        "resolutions": [matrix.cellSize for matrix in tms],
                    },
                    media_type="text/html",
                )
            else:
                return local_templates.TemplateResponse(
                    name="map-form.html",
                    context={
                        "request": request,
                    },
                    media_type="text/html",
                )
