# Changelog

## unreleased

* nothing

# 0.7.0

* Add support for icechunk datasets by @jbusecke ([#103](https://github.com/developmentseed/titiler-xarray/pull/103)) 
    
# 0.6.0

* Convert Lambda to a containerized function
* Enable OpenTelemetry + X-Ray tracing

## 0.5.0

* Add ipykernel and uvicorn to uv --dev by @maxrjones in https://github.com/developmentseed/titiler-multidim/pull/91
* deps: upgrade titiler, Python 3.12, boto3/botocore/aiobotocore by @hrodmn in https://github.com/developmentseed/titiler-multidim/pull/93
* chore: add action to deploy the dev stack by @hrodmn in https://github.com/developmentseed/titiler-multidim/pull/94

## 0.4.0

* add custom host + root_path config by @hrodmn in https://github.com/developmentseed/titiler-multidim/pull/87

## 0.3.1

* Upgrade to `titiler>=0.21,<0.22`
* Use default `map.html` template from `titiler.core` for the `/map` endpoint instead of a custom one

## 0.3.0

* Import `titiler.xarray` (from [`titiler repo`](https://github.com/developmentseed/titiler)) ([#72](https://github.com/developmentseed/titiler-xarray/pull/72))
* Rename the package to `titiler.multidim` ([#72](https://github.com/developmentseed/titiler-xarray/pull/72)) **breaking change**
* Drop support for kerchunk reference files ([#72](https://github.com/developmentseed/titiler-xarray/pull/72)) **breaking change**
* Drop support for experimental `multiscale` zarr group zoom level functionality ([#72](https://github.com/developmentseed/titiler-xarray/pull/72)) **breaking change**
* Remove default `WebMercatorQuad` tile matrix set in `/tiles`, `/tilesjson.json`, `/map` and `/WMTSCapabilities.xml` endpoints (with upgrade to `titiler.core>=0.19`) **breaking change**
* Use `uv` for managing dependencies [#74](https://github.com/developmentseed/titiler-xarray/pull/74)
* Slim down the Lambda asset package size [#74](https://github.com/developmentseed/titiler-xarray/pull/74)
  * run `strip` on compiled C/C++ extensions (except `numpy.libs`)

## v0.2.0

### Improved pyramid support through group parameter

* Add support for a group parameter in `/histogram` route.
* Catch `zarr.errors.GroupNotFoundError` and raise 422 in the `tiles` route. When the `multiscale` parameter is `true` but the zoom level doesn't exist as a group in the zarr hierarchy, this error is raised.

### Add metadata caching via redis cache and AWS elasticache

* Added metadata caching via redis cache and AWS elasticache.
* Use fakeredis for cache in tests.
* Remove [starlette-cramjam CompressionMiddleware](https://github.com/developmentseed/starlette-cramjam).
* Address more cases of protocol/engine combinations in reader.py#get_filesystem.
* Moved cftime and pandas requirements from Dockerfile to pyproject.toml.

## v0.1.1

Support for NetCDF and making consolidated metadata optional. See <https://github.com/developmentseed/titiler-xarray/pull/39>.

[Performance results between prod (v0.1.0) and dev (unreleased)](https://github.com/developmentseed/tile-benchmarking/blob/bd1703209bbeab501f312d99fc51fda6bd419bf9/03-e2e/compare-prod-dev.ipynb).

* Performance for supported datasets is about the same.
* Unsupported datasets in v0.1.0 (netcdf and unconsolidated metadata) reported 100% errors in prod and 0% in dev (expected).
  * NetCDF Dataset: pr_day_ACCESS-CM2_historical_r1i1p1f1_gn_1950.nc
  * Unconsolidated metadata dataset: prod-giovanni-cache-GPM_3IMERGHH_06_precipitationCal

## v0.1.0 (2023-10-11)

Initial release of the project.
