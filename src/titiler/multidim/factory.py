"""TiTiler Xarray mosaic factory."""

from collections.abc import Callable
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

import jinja2
import numpy as np
from attrs import define
from fastapi import Body, Depends, Path, Query
from geojson_pydantic.features import Feature, FeatureCollection
from rio_tiler.constants import WGS84_CRS
from rio_tiler.models import Info
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates
from titiler.core.dependencies import (
    BidxParams,
    CoordCRSParams,
    CoverScaleParams,
    CRSParams,
    DefaultDependency,
    DstCRSParams,
)
from titiler.core.errors import BadRequestError
from titiler.core.models.mapbox import TileJSON
from titiler.core.models.responses import InfoGeoJSON, Point, StatisticsGeoJSON
from titiler.core.resources.enums import ImageType
from titiler.core.resources.responses import GeoJSONResponse, JSONResponse
from titiler.core.utils import bounds_to_geometry
from titiler.mosaic.factory import MOSAIC_THREADS, MosaicTilerFactory
from titiler.xarray.dependencies import (
    DatasetParams,
    PartFeatureParams,
    XarrayIOParams,
    XarrayParams,
)

from titiler.multidim.mosaic import XarrayMosaicBackend
from titiler.multidim.reader import XarrayReader


def DatasetPathParams(
    url: list[str] = Query(
        min_length=1,
        max_length=20,
        description="One to twenty ordered Xarray dataset URLs.",
    ),
) -> list[str]:
    """Return the ordered Xarray source URLs."""
    return url


@define(kw_only=True)
class XarrayMosaicTilerFactory(MosaicTilerFactory):
    """Xarray Mosaic Tiler Factory"""

    backend: type[XarrayMosaicBackend] = XarrayMosaicBackend
    dataset_reader: type[XarrayReader] = XarrayReader
    path_dependency: Callable[..., list[str]] = DatasetPathParams
    reader_dependency: type[DefaultDependency] = XarrayParams
    layer_dependency: type[DefaultDependency] = BidxParams
    dataset_dependency: type[DefaultDependency] = DatasetParams
    img_part_dependency: type[DefaultDependency] = PartFeatureParams

    def register_routes(self) -> None:
        """Register the Xarray route surface, without MosaicJSON asset routes."""
        self.info()
        self.tilesets()
        self.tile()
        self.map_viewer()
        self.tilejson()
        self.point()
        self.part()
        self.statistics()
        self.variables()

    def info(self) -> None:
        """Register Xarray-shaped info endpoints."""

        @self.router.get(
            "/info",
            response_model=Info,
            response_model_exclude_none=True,
            response_class=JSONResponse,
            responses={200: {"description": "Return dataset's basic info."}},
            operation_id=f"{self.operation_prefix}getInfo",
        )
        def info_endpoint(
            src_path=Depends(self.path_dependency),
            reader_params=Depends(self.reader_dependency),
            show_times: Annotated[
                bool | None,
                Query(description="Show info about the time dimension"),
            ] = None,
        ):
            """Return native source info or aggregate mosaic info."""
            with self.backend(
                src_path,
                reader=self.dataset_reader,
                reader_options=reader_params.as_dict(),
            ) as src:
                info = src.info()
                if show_times and len(src_path) == 1:
                    with self.dataset_reader(
                        src_path[0], **reader_params.as_dict()
                    ) as source:
                        if "time" in source.input.dims:
                            info["count"] = len(source.input.time)
                            info["times"] = [
                                str(value.data) for value in source.input.time
                            ]
            return info

        @self.router.get(
            "/info.geojson",
            response_model=InfoGeoJSON,
            response_model_exclude_none=True,
            response_class=GeoJSONResponse,
            responses={
                200: {
                    "content": {"application/geo+json": {}},
                    "description": "Return dataset's basic info as a GeoJSON feature.",
                }
            },
            operation_id=f"{self.operation_prefix}getInfoGeoJSON",
        )
        def info_geojson(
            src_path=Depends(self.path_dependency),
            reader_params=Depends(self.reader_dependency),
            crs=Depends(CRSParams),
        ):
            """Return native source info or aggregate mosaic info as GeoJSON."""
            with self.backend(
                src_path,
                reader=self.dataset_reader,
                reader_options=reader_params.as_dict(),
            ) as src:
                bounds = src.get_geographic_bounds(crs or WGS84_CRS)
                return Feature(
                    type="Feature",
                    bbox=bounds,
                    geometry=bounds_to_geometry(bounds),
                    properties=src.info(),
                )

    def tilejson(self) -> None:  # noqa: C901
        """Register a TileJSON endpoint with Xarray metadata."""

        @self.router.get(
            "/{tileMatrixSetId}/tilejson.json",
            response_model=TileJSON,
            responses={200: {"description": "Return a tilejson"}},
            response_model_exclude_none=True,
            operation_id=f"{self.operation_prefix}getTileJSON",
        )
        def tilejson(
            request: Request,
            tileMatrixSetId: Annotated[  # type: ignore
                Literal[tuple(self.supported_tms.list())],
                Path(description="Identifier selecting a supported TileMatrixSetId."),
            ],
            tilesize: Annotated[
                int | None,
                Query(gt=0, description="Tilesize in pixels. Default to 512."),
            ] = 512,
            tile_format: Annotated[
                ImageType | None,
                Query(description="Output image format."),
            ] = None,
            minzoom: Annotated[
                int | None, Query(description="Overwrite minzoom.")
            ] = None,
            maxzoom: Annotated[
                int | None, Query(description="Overwrite maxzoom.")
            ] = None,
            src_path=Depends(self.path_dependency),
            reader_params=Depends(self.reader_dependency),
        ):
            """Return a TileJSON document for the requested sources."""
            route_params: dict[str, Any] = {
                "z": "{z}",
                "x": "{x}",
                "y": "{y}",
                "tileMatrixSetId": tileMatrixSetId,
            }
            if tile_format:
                route_params["format"] = tile_format.value
            tiles_url = self.url_for(request, "tile", **route_params)
            qs = [
                (key, value)
                for key, value in request.query_params._list
                if key.lower()
                not in {"tilematrixsetid", "tile_format", "minzoom", "maxzoom"}
            ]
            if "tilesize" not in request.query_params:
                qs.append(("tilesize", str(tilesize)))
            tiles_url += f"?{urlencode(qs)}"

            tms = self.supported_tms.get(tileMatrixSetId)
            with self.backend(
                src_path,
                tms=tms,
                reader=self.dataset_reader,
                reader_options=reader_params.as_dict(),
            ) as src:
                info = src.info()
                return {
                    "bounds": src.get_geographic_bounds(tms.rasterio_geographic_crs),
                    "minzoom": minzoom if minzoom is not None else src.minzoom,
                    "maxzoom": maxzoom if maxzoom is not None else src.maxzoom,
                    "tiles": [tiles_url],
                    "raster_layers": self.get_renders(src),
                    "band_descriptions": info.get("band_descriptions"),
                    "data_type": info.get("dtype"),
                    "minmax": info.get("minmax"),
                }

    def point(self) -> None:
        """Register a strategy-composited Xarray point endpoint."""

        @self.router.get(
            "/point/{lon},{lat}",
            response_model=Point,
            response_class=JSONResponse,
            responses={200: {"description": "Return a value for a point"}},
            operation_id=f"{self.operation_prefix}getDataForPoint",
        )
        def point(
            lon: float,
            lat: float,
            src_path=Depends(self.path_dependency),
            reader_params=Depends(self.reader_dependency),
            coord_crs=Depends(CoordCRSParams),
            layer_params=Depends(self.layer_dependency),
            dataset_params=Depends(self.dataset_dependency),
            pixel_selection=Depends(self.pixel_selection_dependency),
        ):
            """Return one point response using the requested mosaic strategy."""
            with self.backend(
                src_path,
                reader=self.dataset_reader,
                reader_options=reader_params.as_dict(),
            ) as src:
                point_data, _ = src.point(
                    lon,
                    lat,
                    coord_crs=coord_crs or WGS84_CRS,
                    pixel_selection=pixel_selection,
                    threads=MOSAIC_THREADS,
                    **layer_params.as_dict(),
                    **dataset_params.as_dict(),
                )
            return {
                "coordinates": [lon, lat],
                "values": point_data.array.tolist(),
                "band_names": point_data.band_names,
                "band_descriptions": point_data.band_descriptions,
            }

    def statistics(self) -> None:
        """Register strategy-composited statistics without mosaic-only fields."""

        @self.router.post(
            "/statistics",
            response_model=StatisticsGeoJSON,
            response_model_exclude_none=True,
            response_class=GeoJSONResponse,
            responses={
                200: {
                    "content": {"application/geo+json": {}},
                    "description": "Return statistics for geojson features.",
                }
            },
            operation_id=f"{self.operation_prefix}postStatisticsForGeoJSON",
        )
        def geojson_statistics(
            geojson: Annotated[
                FeatureCollection | Feature,
                Body(description="GeoJSON Feature or FeatureCollection."),
            ],
            src_path=Depends(self.path_dependency),
            reader_params=Depends(self.reader_dependency),
            coord_crs=Depends(CoordCRSParams),
            dst_crs=Depends(DstCRSParams),
            layer_params=Depends(self.layer_dependency),
            dataset_params=Depends(self.dataset_dependency),
            pixel_selection=Depends(self.pixel_selection_dependency),
            image_params=Depends(self.img_part_dependency),
            cover_scale=Depends(CoverScaleParams),
            post_process=Depends(self.process_dependency),
            stats_params=Depends(self.stats_dependency),
            histogram_params=Depends(self.histogram_dependency),
        ):
            """Calculate statistics from composited feature pixels."""
            collection = (
                FeatureCollection(type="FeatureCollection", features=[geojson])
                if isinstance(geojson, Feature)
                else geojson
            )
            with self.backend(
                src_path,
                reader=self.dataset_reader,
                reader_options=reader_params.as_dict(),
            ) as src:
                for feature in collection.features:
                    shape = feature.model_dump(exclude_none=True)
                    image, _ = src.feature(
                        shape,
                        shape_crs=coord_crs or WGS84_CRS,
                        dst_crs=dst_crs,
                        align_bounds_with_dataset=True,
                        pixel_selection=pixel_selection,
                        threads=MOSAIC_THREADS,
                        **layer_params.as_dict(),
                        **dataset_params.as_dict(),
                        **image_params.as_dict(),
                    )
                    coverage = image.get_coverage_array(
                        shape,
                        shape_crs=coord_crs or WGS84_CRS,
                        cover_scale=cover_scale,
                    )
                    if post_process:
                        image = post_process(image)
                    feature.properties = feature.properties or {}
                    feature.properties["statistics"] = image.statistics(
                        **stats_params.as_dict(),
                        hist_options=histogram_params.as_dict(),
                        coverage=coverage,
                    )
            return (
                collection.features[0] if isinstance(geojson, Feature) else collection
            )

        @self.router.get(
            "/histogram",
            response_class=JSONResponse,
            responses={200: {"description": "Return histogram for this data variable"}},
            response_model_exclude_none=True,
        )
        def histogram(
            src_path=Depends(self.path_dependency),
            reader_params=Depends(self.reader_dependency),
            pixel_selection=Depends(self.pixel_selection_dependency),
        ):
            """Return a native or strategy-composited ten-bucket histogram."""
            if len(src_path) == 1:
                with self.dataset_reader(src_path[0], **reader_params.as_dict()) as src:
                    values = src.input.values[~np.isnan(src.input)]
            else:
                with self.backend(
                    src_path,
                    reader=self.dataset_reader,
                    reader_options=reader_params.as_dict(),
                ) as src:
                    image, _ = src.part(
                        src.bounds,
                        pixel_selection=pixel_selection,
                        threads=MOSAIC_THREADS,
                    )
                    values = image.array.compressed()
            counts, edges = np.histogram(values, bins=10)
            return [
                {"bucket": edges[index : index + 2].tolist(), "value": count}
                for index, count in enumerate(counts.tolist())
            ]

    def variables(self) -> None:
        """Register the shared-variable endpoint."""

        @self.router.get(
            "/variables",
            response_class=JSONResponse,
            responses={200: {"description": "Return dataset variables."}},
        )
        def get_variables(
            src_path=Depends(self.path_dependency),
            io_params=Depends(XarrayIOParams),
        ) -> list[str]:
            """Return variables shared by every requested source."""
            variables = [
                self.dataset_reader.list_variables(
                    src_path=path,
                    group=io_params.group,
                    decode_times=io_params.decode_times,
                )
                for path in src_path
            ]
            if any(set(current) != set(variables[0]) for current in variables[1:]):
                raise BadRequestError(
                    "Requested Xarray sources have different variables."
                )
            return variables[0]

    def map_viewer(self) -> None:
        """Register /map.html and redirect legacy /map URLs."""

        @self.router.get("/{tileMatrixSetId}/map", include_in_schema=False)
        def map_redirect(request: Request, tileMatrixSetId: str):
            """Redirect legacy map URLs."""
            url = f"{request.url.path}.html"
            if request.url.query:
                url += f"?{request.url.query}"
            return RedirectResponse(url)

        @self.router.get("/{tileMatrixSetId}/map.html", response_class=HTMLResponse)
        def map_viewer(
            request: Request,
            tileMatrixSetId: Annotated[  # type: ignore
                Literal[tuple(self.supported_tms.list())],
                "Identifier selecting one of the supported TileMatrixSetIds",
            ],
            url: Annotated[
                list[str] | None,
                Query(description="Ordered Xarray dataset URLs"),
            ] = None,
            variable: Annotated[
                str | None, Query(description="Xarray Variable")
            ] = None,
            group: Annotated[
                str | None,
                Query(description="Select a specific zarr group."),
            ] = None,
            decode_times: Annotated[
                bool,
                Query(description="Whether to decode times"),
            ] = True,
            sel: Annotated[
                list[str] | None,
                Query(description="Xarray indexing using `{dimension}={value}`."),
            ] = None,
            tile_format: Annotated[
                ImageType | None,
                Query(description="Default output image format."),
            ] = None,
            tilesize: Annotated[
                int, Query(gt=0, description="Tilesize in pixels.")
            ] = 256,
            minzoom: Annotated[
                int | None, Query(description="Overwrite minzoom.")
            ] = None,
            maxzoom: Annotated[
                int | None, Query(description="Overwrite maxzoom.")
            ] = None,
            post_process=Depends(self.process_dependency),
            colormap=Depends(self.colormap_dependency),
            render_params=Depends(self.render_dependency),
            dataset_params=Depends(self.dataset_dependency),
        ):
            """Return the existing map viewer or form."""
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
                query = list(request.query_params._list)
                if "tilesize" not in request.query_params:
                    query.append(("tilesize", tilesize))
                tilejson_url += f"?{urlencode(query)}"

                point_url = self.url_for(request, "point", lon="{lon}", lat="{lat}")
                point_query = [
                    (key, value)
                    for key, value in request.query_params._list
                    if key.lower()
                    not in {
                        "tilesize",
                        "tile_format",
                        "minzoom",
                        "maxzoom",
                        "buffer",
                        "padding",
                        "colormap",
                        "colormap_name",
                    }
                ]
                point_url += f"?{urlencode(point_query)}"
                tms = self.supported_tms.get(tileMatrixSetId)
                return titiler_templates.TemplateResponse(
                    request,
                    name="map.html",
                    context={
                        "request": request,
                        "tilejson_endpoint": tilejson_url,
                        "point_endpoint": point_url,
                        "tms": tms,
                        "resolutions": [matrix.cellSize for matrix in tms],
                    },
                    media_type="text/html",
                )
            return local_templates.TemplateResponse(
                request,
                name="map-form.html",
                context={"request": request},
                media_type="text/html",
            )
