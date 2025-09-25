#!/usr/bin/env python3
"""
Benchmark script for titiler-multidim Lambda performance testing.

This script tests Lambda performance by:
1. Warming up with a tilejson request to get dataset bounds
2. Generating tile coordinates that intersect the dataset bounds using morecantile
3. Measuring tile loading performance at the specified zoom level
4. Providing comprehensive statistics

Usage Examples:
    # Basic usage with default parameters (zoom 4, hardcoded dataset)
    uv run benchmark.py --api-url https://your-lambda-url.amazonaws.com

    # Specify custom zoom level
    uv run benchmark.py --api-url https://your-lambda-url.amazonaws.com --zoom 6

    # Use dataset parameters from JSON file
    uv run benchmark.py --api-url https://your-lambda-url.amazonaws.com --dataset-json dataset.json --zoom 5

    # Use dataset parameters from STDIN
    echo '{"url": "s3://bucket/data.zarr", "variable": "temp"}' | uv run benchmark.py --api-url https://your-lambda-url.amazonaws.com --dataset-stdin

    # Export results to CSV
    uv run benchmark.py --api-url https://your-lambda-url.amazonaws.com --zoom 4 --export-csv

    # Combine multiple options
    uv run benchmark.py --api-url https://your-lambda-url.amazonaws.com --dataset-json my-dataset.json --zoom 7 --max-concurrent 30 --export-csv

Dataset JSON format:
    {
        "url": "s3://bucket/path/to/dataset.zarr",
        "variable": "temperature",
        "sel": "time=2023-01-01T00:00:00.000000000",
        "rescale": "250,350",
        "colormap_name": "viridis"
    }
"""

import argparse
import asyncio
import csv
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx
import morecantile

# Test parameters
DATASET_PARAMS = {
    "url": "s3://mur-sst/zarr-v1",
    "variable": "analysed_sst",
    "sel": "time=2018-03-02T09:00:00.000000000",
    "rescale": "250,350",
    "colormap_name": "viridis",
}

# Default zoom level (can be overridden by command line argument)
DEFAULT_ZOOM_LEVEL = 4


def load_dataset_params(
    json_file: Optional[str] = None, use_stdin: bool = False
) -> Dict:
    """Load dataset parameters from JSON file, STDIN, or use defaults."""
    if use_stdin:
        try:
            return json.load(sys.stdin)
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON from STDIN: {e}")
            sys.exit(1)
    elif json_file:
        try:
            with open(json_file, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"Error: Dataset JSON file '{json_file}' not found")
            sys.exit(1)
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON from file '{json_file}': {e}")
            sys.exit(1)
    else:
        return DATASET_PARAMS


def get_tiles_for_bounds(bounds: List[float], zoom: int) -> List[Tuple[int, int]]:
    """Generate tile coordinates for the given bounds and zoom level using morecantile."""
    west, south, east, north = bounds

    tms = morecantile.tms.get("WebMercatorQuad")

    # Generate tiles that intersect with the bounds
    tiles = list(tms.tiles(west, south, east, north, [zoom]))

    # Return as (x, y) coordinate tuples
    return [(tile.x, tile.y) for tile in tiles]


@dataclass
class BenchmarkResult:
    """Container for benchmark results."""

    zoom_level: int = 0
    warmup_time: float = 0.0
    warmup_success: bool = False
    tile_times: List[float] = field(default_factory=list)
    tile_failures: List[Tuple[int, int]] = field(default_factory=list)
    tile_coords: List[Tuple[int, int]] = field(default_factory=list)
    total_runtime: float = 0.0
    start_time: float = 0.0


async def fetch_tilejson(
    client: httpx.AsyncClient, api_url: str, dataset_params: Dict
) -> Tuple[float, bool, Optional[Dict]]:
    """Fetch tilejson to warm up the Lambda and get bounds information."""
    url = f"{api_url}/WebMercatorQuad/tilejson.json"

    start_time = time.time()
    try:
        response = await client.get(url, params=dataset_params, timeout=60.0)
        elapsed = time.time() - start_time

        if response.status_code == 200:
            return elapsed, True, response.json()
        else:
            print(f"Tilejson request failed with status {response.status_code}")
            return elapsed, False, None

    except Exception as e:
        elapsed = time.time() - start_time
        print(f"Tilejson request failed: {e}")
        return elapsed, False, None


async def fetch_tile(
    client: httpx.AsyncClient,
    api_url: str,
    x: int,
    y: int,
    zoom: int,
    dataset_params: Dict,
    semaphore: asyncio.Semaphore,
) -> Tuple[int, int, float, bool]:
    """Fetch a single tile and return timing information."""
    async with semaphore:
        url = f"{api_url}/tiles/WebMercatorQuad/{zoom}/{x}/{y}.png"

        start_time = time.time()
        try:
            response = await client.get(url, params=dataset_params, timeout=30.0)
            elapsed = time.time() - start_time
            success = response.status_code == 200
            return x, y, elapsed, success

        except Exception as e:
            elapsed = time.time() - start_time
            print(f"Tile {x},{y} failed: {e}")
            return x, y, elapsed, False


async def benchmark_tiles(
    client: httpx.AsyncClient,
    api_url: str,
    zoom: int,
    dataset_params: Dict,
    max_concurrent: int = 20,
) -> BenchmarkResult:
    """Run the complete benchmark test."""
    result = BenchmarkResult()
    result.zoom_level = zoom
    result.start_time = time.time()

    # Step 1: Warmup with tilejson request and get bounds
    print("🚀 Warming up Lambda with tilejson request...")
    warmup_time, warmup_success, tilejson_data = await fetch_tilejson(
        client, api_url, dataset_params
    )
    result.warmup_time = warmup_time
    result.warmup_success = warmup_success

    if warmup_success:
        print(f"✅ Warmup successful in {warmup_time:.2f}s")

        # Display dataset information from tilejson
        if tilejson_data:
            print("🗺️  Dataset info:")
            if "bounds" in tilejson_data:
                print(f"    Bounds: {tilejson_data['bounds']}")
            if "center" in tilejson_data:
                print(f"    Center: {tilejson_data['center']}")
            if "minzoom" in tilejson_data and "maxzoom" in tilejson_data:
                print(
                    f"    Zoom range: {tilejson_data['minzoom']} - {tilejson_data['maxzoom']}"
                )
            print("📋 Full TileJSON response:")
            print(f"    {json.dumps(tilejson_data, indent=2)}")
            print()
    else:
        print(f"❌ Warmup failed after {warmup_time:.2f}s")
        return result

    # Step 2: Extract bounds and generate tile coordinates
    if not tilejson_data or "bounds" not in tilejson_data:
        print("❌ No bounds found in tilejson response, falling back to world bounds")
        bounds = [-180.0, -90.0, 180.0, 90.0]  # World bounds
    else:
        bounds = tilejson_data["bounds"]

    print(f"📍 Using bounds: {bounds}")
    print(f"📍 Generating tile coordinates for zoom level {zoom}...")

    tile_coords = get_tiles_for_bounds(bounds, zoom)
    result.tile_coords = tile_coords  # Store for CSV export

    print(f"📍 Found {len(tile_coords)} tiles intersecting dataset bounds")

    # Step 3: Fetch all tiles concurrently
    print(f"🌍 Fetching zoom {zoom} tiles (max {max_concurrent} concurrent)...")
    semaphore = asyncio.Semaphore(max_concurrent)

    tasks = [
        fetch_tile(client, api_url, x, y, zoom, dataset_params, semaphore)
        for x, y in tile_coords
    ]

    # Show progress as tiles complete
    completed = 0
    progress_interval = max(1, len(tile_coords) // 10) if len(tile_coords) >= 10 else 1

    for task in asyncio.as_completed(tasks):
        x, y, elapsed, success = await task
        completed += 1

        if success:
            result.tile_times.append(elapsed)
        else:
            result.tile_failures.append((x, y))

        # Show progress
        if completed % progress_interval == 0 or completed == len(tile_coords):
            progress = (completed / len(tile_coords)) * 100
            print(f"  Progress: {progress:.0f}% ({completed}/{len(tile_coords)} tiles)")

    result.total_runtime = time.time() - result.start_time
    return result


def print_summary(result: BenchmarkResult):
    """Print comprehensive benchmark statistics."""
    print("\n" + "=" * 60)
    print("🏁 BENCHMARK SUMMARY")
    print("=" * 60)

    # Warmup stats
    print("Warmup Request:")
    print(f"  Status: {'✅ Success' if result.warmup_success else '❌ Failed'}")
    print(f"  Time: {result.warmup_time:.2f}s")
    print()

    # Overall stats
    print(f"Total Runtime: {result.total_runtime:.2f}s")
    print()

    # Tile request stats
    total_tiles = len(result.tile_times) + len(result.tile_failures)
    success_count = len(result.tile_times)
    failure_count = len(result.tile_failures)
    success_rate = (success_count / total_tiles * 100) if total_tiles > 0 else 0

    print("Tile Request Summary:")
    print(f"  Zoom level: {result.zoom_level}")
    print(f"  Total tiles: {total_tiles}")
    print(f"  Successful: {success_count} ({success_rate:.1f}%)")
    print(f"  Failed: {failure_count} ({100 - success_rate:.1f}%)")
    print()

    if result.tile_times:
        # Response time statistics
        avg_time = statistics.mean(result.tile_times)
        min_time = min(result.tile_times)
        max_time = max(result.tile_times)
        median_time = statistics.median(result.tile_times)

        # Calculate percentiles
        sorted_times = sorted(result.tile_times)
        p95_idx = int(0.95 * len(sorted_times))
        p95_time = sorted_times[p95_idx]

        print("Response Time Analysis:")
        print(f"  Average: {avg_time:.3f}s")
        print(f"  Minimum: {min_time:.3f}s")
        print(f"  Maximum: {max_time:.3f}s")
        print(f"  Median: {median_time:.3f}s")
        print(f"  95th percentile: {p95_time:.3f}s")
        print()

        # Throughput metrics
        tile_loading_time = result.total_runtime - result.warmup_time
        throughput = success_count / tile_loading_time if tile_loading_time > 0 else 0

        print("Throughput Metrics:")
        print(f"  Tiles per second: {throughput:.1f}")
        print(f"  Tile loading time: {tile_loading_time:.2f}s")

    if result.tile_failures:
        print(
            f"\nFailed Tiles: {result.tile_failures[:10]}{'...' if len(result.tile_failures) > 10 else ''}"
        )


def export_csv(result: BenchmarkResult, filename: str = "benchmark_results.csv"):
    """Export detailed results to CSV."""
    if not result.tile_coords:
        print("❌ No tile coordinates available for CSV export")
        return

    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["tile_x", "tile_y", "response_time_s", "success"])

        # Create mapping of failed tiles
        failure_coords = set(result.tile_failures)

        # Keep track of successful tiles in order (they align with tile_times)
        tile_idx = 0

        for x, y in result.tile_coords:
            if (x, y) in failure_coords:
                writer.writerow([x, y, "N/A", False])
            elif tile_idx < len(result.tile_times):
                writer.writerow([x, y, f"{result.tile_times[tile_idx]:.3f}", True])
                tile_idx += 1
            else:
                # This shouldn't happen, but just in case
                writer.writerow([x, y, "N/A", "Unknown"])

    print(f"📊 Detailed results exported to {filename}")


async def main():
    """Main benchmark execution."""
    parser = argparse.ArgumentParser(
        description="Benchmark titiler-multidim Lambda performance"
    )
    parser.add_argument("--api-url", required=True, help="Lambda API URL")
    parser.add_argument(
        "--max-concurrent", type=int, default=20, help="Maximum concurrent requests"
    )
    parser.add_argument(
        "--export-csv", action="store_true", help="Export results to CSV"
    )
    parser.add_argument(
        "--zoom", type=int, default=4, help="Zoom level for tile requests (default: 4)"
    )
    parser.add_argument(
        "--dataset-json", help="JSON file path containing dataset parameters"
    )
    parser.add_argument(
        "--dataset-stdin",
        action="store_true",
        help="Read dataset parameters from STDIN as JSON",
    )

    args = parser.parse_args()

    # Load dataset parameters
    dataset_params = load_dataset_params(args.dataset_json, args.dataset_stdin)

    # Override with environment variable if set
    api_url = os.environ.get("API_URL", args.api_url)

    print(f"🎯 Benchmarking Lambda at: {api_url}")
    print("📊 Dataset parameters:")
    for key, value in dataset_params.items():
        print(f"    {key}: {value}")
    print(f"🔍 Zoom level: {args.zoom}")
    print(f"⚡ Max concurrent requests: {args.max_concurrent}")
    print()

    # Configure httpx client with appropriate timeouts
    timeout = httpx.Timeout(60.0, connect=10.0)
    limits = httpx.Limits(max_connections=args.max_concurrent * 2)

    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        result = await benchmark_tiles(
            client, api_url, args.zoom, dataset_params, args.max_concurrent
        )

        print_summary(result)

        if args.export_csv:
            export_csv(result)


if __name__ == "__main__":
    asyncio.run(main())
