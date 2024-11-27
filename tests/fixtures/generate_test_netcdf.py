"""Create NetCDF fixture."""

import numpy as np
from netCDF4 import Dataset

# File name
filename = "testfile.nc"

# Create a new NetCDF file
rootgrp = Dataset(f"tests/fixtures/{filename}", "w", format="NETCDF4")

# Create dimensions
res = 1
lat_min, lat_max = -90, 90
lat_n = lat_max - lat_min
lon_min, lon_max = -180, 180
lon_n = lon_max - lon_min
time = rootgrp.createDimension("time", 5)
lat = rootgrp.createDimension("lat", lat_n)
lon = rootgrp.createDimension("lon", lon_n)

# Create variables
times = rootgrp.createVariable("time", "f8", ("time",))
lats = rootgrp.createVariable("lat", "f4", ("lat",))
lons = rootgrp.createVariable("lon", "f4", ("lon",))
data = rootgrp.createVariable(
    "data",
    "f4",
    (
        "time",
        "lat",
        "lon",
    ),
    zlib=True,
)

# Fill variables with data
times[:] = np.arange(5)
lats[:] = np.linspace(lat_min + res / 2, lat_max - res / 2, lat_n)
lons[:] = np.linspace(lon_min + res / 2, lon_max - res / 2, lon_n)
data[:, :, :] = np.random.randint(-128, 127, size=(5, lat_n, lon_n), dtype=np.int8)

# Add some global attributes
rootgrp.description = "Test netCDF file with compressed data"
rootgrp.history = "Created " + np.datetime64("today", "D").astype(str)

# Close the NetCDF file
rootgrp.close()

print(f"{filename} has been created!")
