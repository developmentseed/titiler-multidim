"""Create unconsolidated Zarr fixture."""

import numpy as np
import xarray as xr

# Create some sample data
times = np.arange(10)
xmin, ymin, xmax, ymax = -180, -90, 180, 90
nx = 180
ny = 90
xres = (xmax - xmin) / nx
yres = (ymax - ymin) / ny
lats = np.linspace(ymin + yres / 2, ymax - yres / 2, ny)
lons = np.linspace(xmin + xres / 2, xmax - xres / 2, nx)

data_var1 = np.random.rand(len(times), len(lats), len(lons))
data_var2 = np.random.rand(len(times), len(lats), len(lons))

# Create an xarray Dataset
ds = xr.Dataset(
    {
        "var1": (["time", "lat", "lon"], data_var1),
        "var2": (["time", "lat", "lon"], data_var2),
    },
    coords={"time": times, "lat": lats, "lon": lons},
)

# Save the dataset to a Zarr store
ds.to_zarr("tests/fixtures/unconsolidated.zarr", consolidated=False)

print("Zarr store created!")
