#!/usr/bin/env python3
"""Compare a deployed API's /point values against the source granules.

``verify_store`` (tempo-virtual-zarr-pipeline) proves the Icechunk store
matches the granules byte for byte, and ``test_deployment.py`` proves a
tile renders. This closes the remaining gap: that ``/point`` returns the
*right numbers* — lat/lon indexing, time selection, and CF fill handling
included — by reading pixels straight from the source granule with h5py
and asking the API for the same (lon, lat, time).

For each of ``--times`` randomly sampled store timestamps it locates the
nearest granule via CMR, samples random windows until it finds valid
data, then compares up to ``--pixels`` valid pixels plus one fill pixel.

Requires Earthdata Login (``EARTHDATA_TOKEN``, or ``EARTHDATA_USERNAME``
+ ``EARTHDATA_PASSWORD``, or ``~/.netrc``) and must run in us-west-2:
DAAC temporary S3 credentials are region-locked.

Example (TEMPO HCHO trial store):

    python scripts/check_point_values.py \\
        --api-url https://<your-deployed-endpoint> \\
        --url s3://airquality-data-store-develop/tempo/hcho/v04-trial \\
        --variable vertical_column \\
        --collection C3685897141-LARC_CLOUD
"""

import argparse
import json
import logging
import math
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator
from urllib.parse import urlencode
from urllib.request import urlopen

import cftime
import h5py
import numpy as np

CMR_GRANULES_URL = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
SEARCH_WINDOW = timedelta(hours=2)
WINDOW_TRIES = 8


def pick_pixels(
    window: np.ndarray, fill: Any, rng: np.random.Generator, count: int
) -> list[tuple[int, int, float]]:
    """Pick up to ``count`` valid pixels, plus one fill pixel, from a window.

    Returns ``(row, col, expected)`` triples where ``expected`` is NaN for
    the fill pixel — the value a CF-decoding reader should hand back.
    """
    window = np.asarray(window)
    valid_mask = (
        np.ones(window.shape, bool)
        if fill is None
        # NaN != NaN is True — a NaN _FillValue must land in the fill branch
        else (window != fill) & ~np.isnan(window)
    )
    picks = []
    valid = np.argwhere(valid_mask)
    if len(valid):
        chosen = rng.choice(len(valid), size=min(count, len(valid)), replace=False)
        picks += [(int(r), int(c), float(window[r, c])) for r, c in valid[chosen]]
    if fill is not None:
        fills = np.argwhere(~valid_mask)
        if len(fills):
            r, c = fills[rng.integers(len(fills))]
            picks.append((int(r), int(c), float("nan")))
    return picks


def values_match(api_value: Any, expected: float) -> bool:
    """Compare a /point JSON value against the granule's decoded pixel."""
    if api_value is None or math.isnan(expected):
        return api_value is None and math.isnan(expected)
    return float(api_value) == float(expected)


def source_dataset(h5: h5py.File, name: str) -> h5py.Dataset | None:
    """Find the source dataset behind a flattened variable name."""
    if name in h5:
        return h5[name]  # type: ignore[no-any-return]
    for item in h5.values():
        if isinstance(item, h5py.Group) and name in item:
            return item[name]  # type: ignore[no-any-return]
    return None


def granule_time_matches(
    value: float, units: str, iso: str, tolerance: float = 1.0
) -> bool:
    """Check a granule's raw time value against a store timestamp string."""
    decoded = cftime.num2date(value, units, only_use_cftime_datetimes=False)
    expected = np.datetime64(iso, "us").item()
    return abs((decoded - expected).total_seconds()) <= tolerance


def nearest_granule(collection: str, when: datetime) -> str | None:
    """Ask CMR for the direct-access URL of the granule nearest ``when``."""

    def direct_s3_url(umm: dict) -> str | None:
        for related in umm.get("RelatedUrls", []):
            url = related.get("URL", "")
            if (
                related.get("Type") == "GET DATA VIA DIRECT ACCESS"
                and url.startswith("s3://")
                and url.endswith(".nc")
            ):
                return str(url)
        return None

    def beginning(umm: dict) -> datetime:
        raw = umm["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"]
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)

    params = {
        "collection_concept_id": collection,
        "temporal": f"{(when - SEARCH_WINDOW).isoformat()},"
        f"{(when + SEARCH_WINDOW).isoformat()}",
        "page_size": 10,
    }
    with urlopen(f"{CMR_GRANULES_URL}?{urlencode(params)}", timeout=60) as response:
        items = json.loads(response.read()).get("items", [])
    candidates = [item["umm"] for item in items if direct_s3_url(item["umm"])]
    if not candidates:
        return None
    return direct_s3_url(min(candidates, key=lambda umm: abs(beginning(umm) - when)))


@contextmanager
def open_granule(url: str) -> Iterator[h5py.File]:
    """Open an s3:// granule with h5py over buffered obstore reads."""
    import obstore
    from earthaccess_auth.adapters.obstore import (
        EarthdataS3CredentialProvider,
        resolve_bucket,
    )
    from obstore.store import S3Store

    bucket, key = url.removeprefix("s3://").split("/", 1)
    info = resolve_bucket(bucket)
    if info is None:
        raise SystemExit(f"bucket {bucket!r} is not a known DAAC bucket")
    store = S3Store(
        bucket,
        region=info.region,
        credential_provider=EarthdataS3CredentialProvider(
            info.endpoint, region=info.region
        ),
    )
    with h5py.File(obstore.open_reader(store, key, buffer_size=8 * 1024**2)) as h5:
        yield h5


def fetch_json(url: str) -> Any:
    """GET a JSON document."""
    with urlopen(url, timeout=300) as response:
        return json.loads(response.read())


def check_time(
    api_url: str,
    dataset: dict[str, str],
    collection: str,
    iso: str,
    pixels: int,
    window: int,
    rng: np.random.Generator,
) -> list[str]:
    """Compare API /point values against one granule; return the problems."""
    granule_url = nearest_granule(collection, np.datetime64(iso, "us").item())
    if granule_url is None:
        return [f"{iso}: CMR has no direct-access granule near this time"]
    logging.info("%s: comparing against %s", iso, granule_url)
    problems = []
    with open_granule(granule_url) as h5:
        time_ds = h5["time"]
        units = time_ds.attrs["units"]
        units = units.decode() if isinstance(units, bytes) else str(units)
        if not granule_time_matches(float(time_ds[0]), units, iso):
            return [f"{iso}: granule time {float(time_ds[0])!r} ({units}) differs"]
        variable = dataset["variable"]
        data = source_dataset(h5, variable)
        if data is None:
            return [f"{iso}: variable {variable!r} missing from {granule_url}"]
        latitude = np.asarray(h5["latitude"])
        longitude = np.asarray(h5["longitude"])
        fill = data.attrs.get("_FillValue")
        fill = None if fill is None else np.asarray(fill).ravel()[0]
        ny, nx = data.shape[-2], data.shape[-1]
        picks: list[tuple[int, int, float]] = []
        for _ in range(WINDOW_TRIES):
            r0 = int(rng.integers(0, max(1, ny - window)))
            c0 = int(rng.integers(0, max(1, nx - window)))
            block = np.asarray(data[..., r0 : r0 + window, c0 : c0 + window])
            block = block[0] if block.ndim == 3 else block
            picks = [
                (r0 + r, c0 + c, v) for r, c, v in pick_pixels(block, fill, rng, pixels)
            ]
            if any(not math.isnan(v) for _, _, v in picks):
                break
        if not any(not math.isnan(v) for _, _, v in picks):
            problems.append(
                f"{iso}: no valid pixels found in {WINDOW_TRIES} random windows"
            )
        for r, c, expected in picks:
            lon, lat = float(longitude[c]), float(latitude[r])
            point = fetch_json(
                f"{api_url}/point/{lon!r},{lat!r}?"
                + urlencode({**dataset, "sel": f"time={iso}"})
            )
            value = point["values"][0]
            if isinstance(value, list):  # one band -> possibly nested
                value = value[0]
            if not values_match(value, expected):
                problems.append(
                    f"{iso}: /point at ({lon}, {lat}) returned {value!r}, "
                    f"granule pixel [{r}, {c}] is {expected!r}"
                )
    return problems


def main() -> int:
    """Sample store timestamps and cross-check /point against granules."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--api-url", required=True, help="Deployed API endpoint")
    parser.add_argument("--url", required=True, help="Icechunk/Zarr store URL")
    parser.add_argument("--variable", required=True, help="Store variable name")
    parser.add_argument(
        "--collection", required=True, help="CMR concept id of the source collection"
    )
    parser.add_argument("--times", type=int, default=2, help="Timestamps to sample")
    parser.add_argument("--pixels", type=int, default=4, help="Valid pixels per time")
    parser.add_argument("--window", type=int, default=128, help="Sample window size")
    parser.add_argument("--seed", type=int, default=None, help="RNG seed")
    args = parser.parse_args()

    api_url = args.api_url.rstrip("/")
    dataset = {"url": args.url, "variable": args.variable}
    info = fetch_json(f"{api_url}/info?{urlencode({**dataset, 'show_times': 'true'})}")
    times = info.get("times")
    if not times:
        logging.error("/info returned no times for %s", args.url)
        return 1
    rng = np.random.default_rng(args.seed)
    sampled = [
        times[i]
        for i in rng.choice(len(times), min(args.times, len(times)), replace=False)
    ]

    problems = []
    for iso in sampled:
        try:
            problems += check_time(
                api_url, dataset, args.collection, iso, args.pixels, args.window, rng
            )
        except Exception as error:  # a failed read is a finding, not a crash
            problems.append(f"{iso}: {type(error).__name__}: {error}")

    for problem in problems:
        logging.error(problem)
    if not problems:
        logging.info("all sampled /point values match their source granules")
    return 1 if problems else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
