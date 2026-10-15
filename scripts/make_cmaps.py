"""Generate the custom colormaps shipped in ``src/titiler/multidim/cmap_data``.

Run from the repository root::

    python scripts/make_cmaps.py

``tests/test_cmaps.py`` re-runs the generators and fails if a committed ``.npy``
differs, so edit the stops here, re-run, and commit both.
See ``src/titiler/multidim/cmap_data/README.md`` for sources and the ``rescale``
each colormap expects.
"""

import json
import pathlib

import numpy as np

HERE = pathlib.Path(__file__).parent
OUT = HERE.parent / "src" / "titiler" / "multidim" / "cmap_data"


def _rgba(hex_color: str) -> list[int]:
    h = hex_color.lstrip("#")
    return [int(h[i : i + 2], 16) for i in (0, 2, 4)] + [255]


def _lut(values: np.ndarray, stop_values, stop_colors) -> np.ndarray:
    """256x4 uint8 table: linear interpolation of ``stop_colors`` at ``values``."""
    rgba = np.array([_rgba(c) for c in stop_colors], dtype=float)
    lut = np.stack(
        [np.interp(values, stop_values, rgba[:, k]) for k in range(4)], axis=1
    )
    return np.rint(lut).astype(np.uint8)


def worldview_rainbow_4() -> np.ndarray:
    """NASA Worldview custom palette "Rainbow 4", verbatim.

    255 colors from nasa-gibs/worldview; the top color is repeated to fill 256 rows.
    """
    source = json.loads((HERE / "cmap_sources" / "rainbow_4.json").read_text())
    colors = source["rainbow_4"]["colors"]  # "rrggbbaa"
    lut = np.array(
        [[int(c[i : i + 2], 16) for i in (0, 2, 4, 6)] for c in colors], dtype=np.uint8
    )
    return np.vstack([lut, lut[-1]])


# NOAA Air Quality Forecast Guidance Viewer ozone ramp, one color per 10 ppb from
# 0 to 200 ppb, as sampled from the viewer for NASA-IMPACT/veda-backend#665.
O3_STOPS_PPB = np.arange(0, 201, 10)
O3_STOP_COLORS = [
    "#AFFEB0", "#7EFE7E", "#4BFF4C", "#08F001", "#03C300", "#008700",  # 0-50
    "#E1E103", "#FEBC2E", "#E17411", "#FE7879", "#BE0001",  # 60-100
    "#F0B4E7", "#DC8CD3", "#C864BF", "#B43DAB", "#A02497",  # 110-150
    "#8B1D83", "#781470", "#640D5B", "#500347", "#3C003C",  # 160-200
]  # fmt: skip
O3_MAX_PPB = 200.0

# EPA AQI category upper bounds: 24-hour PM2.5 (ug/m3) paired with 8-hour ozone (ppb).
# PM2.5 borrows the ozone ramp, stretched so each PM2.5 category spans the same
# colors as its ozone category.
PM25_BOUNDS = [0.0, 9.0, 35.4, 55.4, 125.4, 225.4]
O3_BOUNDS = [0.0, 54.0, 70.0, 85.0, 105.0, 200.0]
PM25_MAX = PM25_BOUNDS[-1]


def naqfc_o3() -> np.ndarray:
    """NAQFC ozone ramp over 0-200 ppb (``rescale=0,200`` for ppb, ``0,0.2`` for ppm)."""
    return _lut(np.linspace(0, O3_MAX_PPB, 256), O3_STOPS_PPB, O3_STOP_COLORS)


def naqfc_pm25() -> np.ndarray:
    """NAQFC PM2.5 ramp over 0-225.4 ug/m3 (``rescale=0,225.4``)."""
    pm25 = np.linspace(0, PM25_MAX, 256)
    o3_equivalent = np.interp(pm25, PM25_BOUNDS, O3_BOUNDS)
    return _lut(o3_equivalent, O3_STOPS_PPB, O3_STOP_COLORS)


CMAPS = {
    "worldview_rainbow_4": worldview_rainbow_4,
    "naqfc_o3": naqfc_o3,
    "naqfc_pm25": naqfc_pm25,
}

if __name__ == "__main__":
    for name, build in CMAPS.items():
        lut = build()
        assert lut.shape == (256, 4) and lut.dtype == np.uint8, name
        np.save(OUT / f"{name}.npy", lut)
        print(f"wrote {OUT / name}.npy")
