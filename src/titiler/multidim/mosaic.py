"""Request-scoped Xarray mosaic backend."""

from typing import Any

import attr
from morecantile import Tile
from rasterio.crs import CRS
from rasterio.warp import transform, transform_bounds
from rio_tiler.constants import WGS84_CRS
from rio_tiler.errors import NoAssetFoundError, PointOutsideBounds
from rio_tiler.mosaic.backend import BaseBackend
from rio_tiler.mosaic.reader import mosaic_point_reader
from rio_tiler.types import BBox
from rio_tiler.utils import inherit_rasterio_env
from titiler.core.errors import BadRequestError


@attr.s
class XarrayMosaicBackend(BaseBackend):
    """Mosaic a request-ordered list of compatible Xarray datasets."""

    _asset_bounds: list[BBox] = attr.ib(init=False, factory=list)
    _asset_info: list[dict[str, Any]] = attr.ib(init=False, factory=list)

    def __attrs_post_init__(self) -> None:
        """Validate every requested source and collect its geographic metadata."""
        if not 1 <= len(self.input) <= 20:
            raise BadRequestError("Provide between one and twenty url parameters.")

        signature: tuple[Any, ...] | None = None
        zooms: list[tuple[int, int]] = []
        for asset in self.input:
            with self.reader(asset, tms=self.tms, **self.reader_options) as src:
                info = src.info().model_dump()
                current = (
                    str(src.input.dtype),
                    src.input.rio.count,
                    tuple(
                        dimension
                        for dimension in src.input.dims
                        if dimension not in {src.input.rio.x_dim, src.input.rio.y_dim}
                    ),
                    repr(info["band_metadata"]),
                    info["nodata_type"],
                )
                if signature is not None and current != signature:
                    raise BadRequestError("Requested Xarray sources are incompatible.")

                signature = current
                self._asset_bounds.append(src.get_geographic_bounds(WGS84_CRS))
                self._asset_info.append(info)
                zooms.append((src.minzoom, src.maxzoom))

        self.crs = WGS84_CRS
        self.bounds = self._mosaic_bounds()
        self.minzoom = min(zoom[0] for zoom in zooms)
        self.maxzoom = max(zoom[1] for zoom in zooms)

    def info(self) -> dict[str, Any]:  # type: ignore[override]
        """Return native metadata for one source or shared metadata for a mosaic.

        Deliberately returns plain dicts rather than the base classes'
        Info models: the factory mutates the result (`count`/`times`).
        """
        if len(self._asset_info) == 1:
            return self._asset_info[0]

        info = {
            key: value
            for key, value in self._asset_info[0].items()
            if key not in {"bounds", "crs", "width", "height", "dimensions"}
            and all(other.get(key) == value for other in self._asset_info[1:])
        }
        info.update(
            bounds=self.bounds, crs="http://www.opengis.net/def/crs/EPSG/0/4326"
        )
        return info

    def assets_for_tile(self, x: int, y: int, z: int, **kwargs: Any) -> list[str]:
        """Return request-ordered assets intersecting a tile."""
        return self._assets_for_bounds(self.tms.bounds(Tile(x, y, z)))

    def assets_for_point(
        self,
        lng: float,
        lat: float,
        coord_crs: CRS | None = None,
        **kwargs: Any,
    ) -> list[str]:
        """Return request-ordered assets containing a point."""
        if coord_crs and coord_crs != WGS84_CRS:
            xs, ys = transform(coord_crs, WGS84_CRS, [lng], [lat])
            lng, lat = xs[0], ys[0]
        return self._assets_for_bounds((lng, lat, lng, lat))

    def assets_for_bbox(
        self,
        xmin: float,
        ymin: float,
        xmax: float,
        ymax: float,
        coord_crs: CRS | None = None,
        **kwargs: Any,
    ) -> list[str]:
        """Return request-ordered assets intersecting a bounding box."""
        if coord_crs and coord_crs != WGS84_CRS:
            xmin, ymin, xmax, ymax = transform_bounds(
                coord_crs, WGS84_CRS, xmin, ymin, xmax, ymax
            )
        return self._assets_for_bounds((xmin, ymin, xmax, ymax))

    def point(
        self,
        lon: float,
        lat: float,
        coord_crs: CRS = WGS84_CRS,
        search_options: dict | None = None,
        **kwargs: Any,
    ):
        """Return one rio-tiler-composited point and its contributing assets."""
        assets = self.assets_for_point(
            lon, lat, coord_crs=coord_crs, **(search_options or {})
        )
        if not assets:
            raise NoAssetFoundError(f"No assets found for point ({lon},{lat})")

        @inherit_rasterio_env
        def read_point(asset: str, *args: Any, **options: Any):
            with self.reader(asset, **self.reader_options) as src:
                return src.point(*args, **options)

        return mosaic_point_reader(
            assets,
            read_point,
            lon,
            lat,
            coord_crs=coord_crs,
            allowed_exceptions=(PointOutsideBounds,),
            **kwargs,
        )

    @staticmethod
    def _longitude_intervals(
        west: float, east: float
    ) -> tuple[tuple[float, float], ...]:
        """Split a WGS84 longitude range at the antimeridian when needed."""
        return ((west, east),) if west <= east else ((west, 180), (-180, east))

    def _mosaic_bounds(self) -> BBox:
        """Return the smallest WGS84 bounding box containing every asset."""
        intervals = sorted(
            interval
            for bounds in self._asset_bounds
            for interval in self._longitude_intervals(bounds[0], bounds[2])
        )
        merged: list[tuple[float, float]] = []
        for west, east in intervals:
            if merged and west <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], east))
            else:
                merged.append((west, east))

        if merged == [(-180, 180)]:
            west, east = -180, 180
        else:
            _, west, east = max(
                (
                    ((next_west - current_east) % 360, next_west, current_east)
                    for (_, current_east), (next_west, _) in zip(
                        merged, merged[1:] + merged[:1]
                    )
                ),
                key=lambda gap: gap[0],
            )

        return (
            west,
            min(bounds[1] for bounds in self._asset_bounds),
            east,
            max(bounds[3] for bounds in self._asset_bounds),
        )

    def _assets_for_bounds(self, query_bounds: BBox) -> list[str]:
        """Filter cached WGS84 bounds without changing input order."""
        xmin, ymin, xmax, ymax = query_bounds
        query_longitudes = self._longitude_intervals(xmin, xmax)
        return [
            asset
            for asset, bounds in zip(self.input, self._asset_bounds)
            if bounds[1] <= ymax
            and bounds[3] >= ymin
            and any(
                west <= query_east and east >= query_west
                for west, east in self._longitude_intervals(bounds[0], bounds[2])
                for query_west, query_east in query_longitudes
            )
        ]
