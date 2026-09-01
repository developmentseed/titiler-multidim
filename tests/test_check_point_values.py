"""Tests for the pure logic in scripts/check_point_values.py."""

import io
import sys
from pathlib import Path

import h5py
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import check_point_values as cpv  # noqa: E402

FILL = -1.0e30


def _window():
    values = np.full((4, 4), FILL, dtype="float32")
    values[1, 2] = 5.0
    values[3, 0] = 7.5
    return values


def test_pick_pixels_returns_valid_pixels_with_their_values():
    """Both valid pixels come back with their stored values."""
    picks = cpv.pick_pixels(_window(), FILL, np.random.default_rng(0), count=2)
    valid = [(r, c, v) for r, c, v in picks if not np.isnan(v)]
    assert sorted((r, c) for r, c, _ in valid) == [(1, 2), (3, 0)]
    assert {v for _, _, v in valid} == {5.0, 7.5}


def test_pick_pixels_includes_one_fill_pixel_as_nan():
    """One fill pixel is included, with NaN as its expected value."""
    picks = cpv.pick_pixels(_window(), FILL, np.random.default_rng(0), count=2)
    fills = [(r, c) for r, c, v in picks if np.isnan(v)]
    assert len(fills) == 1
    r, c = fills[0]
    assert _window()[r, c] == FILL


def test_pick_pixels_all_fill_returns_no_valid_pixels():
    """An all-fill window yields no valid picks, signalling a resample."""
    window = np.full((4, 4), FILL, dtype="float32")
    picks = cpv.pick_pixels(window, FILL, np.random.default_rng(0), count=3)
    assert all(np.isnan(v) for _, _, v in picks)


def test_pick_pixels_no_fill_attribute_treats_all_as_valid():
    """Without a fill value every pixel is valid and none is NaN."""
    window = np.arange(4.0, dtype="float32").reshape(2, 2)
    picks = cpv.pick_pixels(window, None, np.random.default_rng(0), count=4)
    assert len(picks) == 4
    assert not any(np.isnan(v) for _, _, v in picks)


def test_values_match():
    """None pairs with NaN; floats must match exactly."""
    assert cpv.values_match(None, float("nan"))
    assert cpv.values_match(5.0, 5.0)
    assert not cpv.values_match(None, 5.0)
    assert not cpv.values_match(5.0, float("nan"))
    assert not cpv.values_match(5.0, 5.0001)
    # float32 source values survive the JSON round trip bit-exactly
    assert cpv.values_match(float(np.float32(1.23e16)), float(np.float32(1.23e16)))


def test_source_dataset_finds_root_and_grouped_names():
    """Flattened names resolve at the root and one group level down."""
    h5 = h5py.File(io.BytesIO(), "w")
    h5["latitude"] = np.arange(3.0)
    h5.create_group("product")
    h5["product/vertical_column_troposphere"] = np.zeros((2, 2))
    assert cpv.source_dataset(h5, "latitude").shape == (3,)
    assert cpv.source_dataset(h5, "vertical_column_troposphere").shape == (2, 2)
    assert cpv.source_dataset(h5, "missing") is None


def test_granule_time_matches():
    """Raw axis seconds match the ISO timestamp within one second."""
    units = "seconds since 1980-01-06T00:00:00Z"
    seconds = 1_470_000_000.0
    iso = "2026-08-05T21:20:00.000000000"  # 1980-01-06 + 1.47e9 s
    assert cpv.granule_time_matches(seconds, units, iso)
    assert cpv.granule_time_matches(seconds + 0.5, units, iso)
    assert not cpv.granule_time_matches(seconds + 600, units, iso)
