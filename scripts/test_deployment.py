#!/usr/bin/env python3
"""Smoke-test public Icechunk datasets against a deployed API."""

import argparse
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

TEST_CASES = (
    (
        "native MUR Icechunk",
        "5/8/13",
        {
            "url": "s3://nasa-eodc-public/icechunk/MUR-JPL-L4-GLOB-v4.1-native-v0/",
            "variable": "analysed_sst",
            "colormap_name": "thermal",
            "rescale": "273,325",
            "decode_times": "true",
        },
    ),
    (
        "virtual MUR Icechunk",
        "5/8/13",
        {
            "url": "s3://nasa-eodc-public/icechunk/MUR-JPL-L4-GLOB-v4.1-virtual-v2-p2",
            "variable": "analysed_sst",
            "colormap_name": "thermal",
            "rescale": "273,325",
            "sel": "time=2024-08-01T09:00:00.000000000",
            "decode_times": "true",
        },
    ),
    (
        "virtual NLDAS Icechunk",
        "5/8/13",
        {
            "url": "s3://nasa-waterinsight/virtual-zarr-store/NLDAS-3-icechunk/",
            "variable": "Tair",
            "colormap_name": "thermal",
            "rescale": "260,325",
            "sel": "time=2001-01-02T00:00:00.000000000",
        },
    ),
    (
        "MUR SST zarr",
        "5/8/13",
        {
            "url": "s3://mur-sst/zarr-v1",
            "variable": "analysed_sst",
            "colormap_name": "thermal",
            "rescale": "273,325",
            "sel": "time=nearest::2002-07-05T00:00:00Z",
            "decode_times": "true",
        },
    ),
)


def main() -> int:
    """Request a tile for each deployed public Icechunk dataset."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True, help="Deployed API endpoint")
    args = parser.parse_args()

    failures = []
    for name, tile, params in TEST_CASES:
        url = f"{args.api_url.rstrip('/')}/tiles/WebMercatorQuad/{tile}.png?{urlencode(params)}"
        logging.info("Requesting %s", name)
        try:
            with urlopen(url, timeout=300) as response:
                if (
                    response.status != 200
                    or response.headers.get_content_type() != "image/png"
                    or response.read(8) != b"\x89PNG\r\n\x1a\n"
                ):
                    failures.append(f"{name}: unexpected response from {url}")
        except (HTTPError, URLError, TimeoutError) as error:
            failures.append(f"{name}: {error}")

    for failure in failures:
        logging.error(failure)
    return 1 if failures else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
