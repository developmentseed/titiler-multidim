"""Create icechunk fixtures (native and later virtual)."""
# TODO: these files could also be generated together with the zarr files using the same data

import numpy as np
import xarray as xr
import icechunk as ic

# Define dimensions and chunk sizes
res = 5
time_dim = 10
lat_dim = 36
lon_dim = 72
chunk_size = {"time": 10, "lat": 10, "lon": 10}

# Create coordinates
time = np.arange(time_dim)
lat = np.linspace(-90.0 + res / 2, 90.0 - res / 2, lat_dim)
lon = np.linspace(-180.0 + res / 2, 180.0 - res / 2, lon_dim)

dtype = np.float64
# Initialize variables with random data
CDD0 = xr.DataArray(
    np.random.rand(time_dim, lat_dim, lon_dim).astype(dtype),
    dims=("time", "lat", "lon"),
    name="CDD0",
)
DISPH = xr.DataArray(
    np.random.rand(time_dim, lat_dim, lon_dim).astype(dtype),
    dims=("time", "lat", "lon"),
    name="DISPH",
)
FROST_DAYS = xr.DataArray(
    np.random.rand(time_dim, lat_dim, lon_dim).astype(dtype),
    dims=("time", "lat", "lon"),
    name="FROST_DAYS",
)
GWETPROF = xr.DataArray(
    np.random.rand(time_dim, lat_dim, lon_dim).astype(dtype),
    dims=("time", "lat", "lon"),
    name="GWETPROF",
)

# Create dataset
ds = xr.Dataset(
    {
        "CDD0": CDD0.chunk(chunk_size),
        "DISPH": DISPH.chunk(chunk_size),
        "FROST_DAYS": FROST_DAYS.chunk(chunk_size),
        "GWETPROF": GWETPROF.chunk(chunk_size),
    },
    coords={"time": time, "lat": lat, "lon": lon},
)
storage = ic.local_filesystem_storage("tests/fixtures/icechunk_native")
config = ic.RepositoryConfig.default()
repo = ic.Repository.create(storage=storage, config=config)
session = repo.writable_session("main")
store = session.store

ds.to_zarr(store, consolidated=False)
session.commit("Add initial data")
