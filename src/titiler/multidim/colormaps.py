"""Custom colormaps, registered alongside rio-tiler's built-ins.

Every ``cmap_data/*.json`` file is registered as the colormap given by its
``name``. Each file has a ``source``, a ``reference`` URL for it, optional
``notes`` on its limits, and either:

- ``stops``: data values mapped to ``#rrggbb`` colors, or
- ``ramp`` and ``breakpoints``: another colormap's ``name``, and data values
  mapped to values on that colormap's stops.

Each colormap is a 256-row table spanning its smallest to largest data value,
which titiler applies after ``rescale`` stretches the data to 0-255, so each one
is only meaningful with the ``rescale`` listed in the README's "Custom
colormaps" section.
"""

import json
from collections.abc import Mapping
from importlib.resources import files
from typing import Any

import numpy as np
from rio_tiler.colormap import cmap as default_cmap
from rio_tiler.colormap import parse_color
from rio_tiler.types import GDALColorMapType


def _points(mapping: Mapping[str, Any]) -> list[float]:
    """The data values (JSON keys) of ``mapping``, as numbers."""
    return [float(k) for k in mapping]


def _colormap_from_ramp(
    stops: Mapping[str, str], values: np.ndarray
) -> GDALColorMapType:
    """Turn ``stops`` into a colormap with one row for each item in ``values``.

    Row ``i`` of the result is the color at ``values[i]``, blended from the
    colors of the two nearest stops when ``values[i]`` falls between them.

    >>> stops = {"0": "#000000", "10": "#FFFFFF"}  # black at 0, white at 10
    >>> _colormap_from_ramp(stops, np.array([0, 5, 10]))
    {0: (0, 0, 0, 255), 1: (128, 128, 128, 255), 2: (255, 255, 255, 255)}
    """
    colors = np.array([parse_color(c) for c in stops.values()])
    channels = [np.interp(values, _points(stops), channel) for channel in colors.T]
    rows = np.rint(np.stack(channels, axis=1)).astype(int).tolist()

    return dict(enumerate(map(tuple, rows)))


def _build(spec: Mapping[str, Any], specs: Mapping[str, Any]) -> GDALColorMapType:
    """Build the 256-row colormap described by one ``cmap_data`` file."""
    if "stops" in spec:
        points = _points(spec["stops"])
        return _colormap_from_ramp(
            spec["stops"], np.linspace(points[0], points[-1], 256)
        )

    points = _points(spec["breakpoints"])
    ramp_values = np.interp(
        np.linspace(points[0], points[-1], 256),
        points,
        list(spec["breakpoints"].values()),
    )
    return _colormap_from_ramp(specs[spec["ramp"]]["stops"], ramp_values)


def _load_specs() -> dict[str, Any]:
    """Read every ``cmap_data`` file, keyed by its ``name``."""
    specs: dict[str, Any] = {}
    for path in (files(__package__) / "cmap_data").iterdir():
        if not path.name.endswith(".json"):
            continue
        spec = json.loads(path.read_text())
        if spec["name"] in specs:
            raise ValueError(f"{path.name} reuses colormap name {spec['name']!r}")
        specs[spec["name"]] = spec
    return specs


SPECS = _load_specs()

cmap = default_cmap.register({name: _build(s, SPECS) for name, s in SPECS.items()})
