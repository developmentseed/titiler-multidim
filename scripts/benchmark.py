#!/usr/bin/env python3
"""
Benchmark script for titiler-multidim Lambda performance testing.

This script tests Lambda performance by:
1. Warming up with a tilejson request
2. Measuring tile loading performance at zoom level 4
3. Providing comprehensive statistics

Usage:
    uv run benchmark.py --api-url https://your-lambda-url.amazonaws.com
"""

import argparse
import asyncio
import csv
import os
import statistics
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import httpx

# Test parameters
DATASET_PARAMS = {
    "url": "s3://mur-sst/zarr-v1",
    "variable": "analysed_sst",
    "sel": "time=2018-03-02T09:00:00.000000000",
    "rescale": "250,350",
    "colormap_name": "viridis",
}

# Zoom 4 covers the world with 16x16 tiles = 256 total tiles
ZOOM_LEVEL = 4
TILES_PER_SIDE = 2**ZOOM_LEVEL  # 16 tiles per side at zoom 4


@dataclass
class BenchmarkResult:
    """Container for benchmark results."""

    warmup_time: float = 0.0
    warmup_success: bool = False
    tile_times: List[float] = []
    tile_failures: List[Tuple[int, int]] = []
    total_runtime: float = 0.0
    start_time: float = 0.0


async def fetch_tilejson(
    client: httpx.AsyncClient, api_url: str
) -> Tuple[float, bool, Optional[Dict]]:
    """Fetch tilejson to warm up the Lambda and get tile URL template."""
    url = f"{api_url}/WebMercatorQuad/tilejson.json"

    start_time = time.time()
    try:
        response = await client.get(url, params=DATASET_PARAMS, timeout=60.0)
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
    semaphore: asyncio.Semaphore,
) -> Tuple[int, int, float, bool]:
    """Fetch a single tile and return timing information."""
    async with semaphore:
        url = f"{api_url}/tiles/WebMercatorQuad/{ZOOM_LEVEL}/{x}/{y}.png"

        start_time = time.time()
        try:
            response = await client.get(url, params=DATASET_PARAMS, timeout=30.0)
            elapsed = time.time() - start_time
            success = response.status_code == 200
            return x, y, elapsed, success

        except Exception as e:
            elapsed = time.time() - start_time
            print(f"Tile {x},{y} failed: {e}")
            return x, y, elapsed, False


async def benchmark_tiles(
    client: httpx.AsyncClient, api_url: str, max_concurrent: int = 20
) -> BenchmarkResult:
    """Run the complete benchmark test."""
    result = BenchmarkResult()
    result.start_time = time.time()

    # Step 1: Warmup with tilejson request
    print("🚀 Warming up Lambda with tilejson request...")
    warmup_time, warmup_success, tilejson_data = await fetch_tilejson(client, api_url)
    result.warmup_time = warmup_time
    result.warmup_success = warmup_success

    if warmup_success:
        print(f"✅ Warmup successful in {warmup_time:.2f}s")
    else:
        print(f"❌ Warmup failed after {warmup_time:.2f}s")
        return result

    # Step 2: Generate all tile coordinates for zoom 4
    print(
        f"📍 Generating {TILES_PER_SIDE}x{TILES_PER_SIDE} = {TILES_PER_SIDE**2} tile coordinates..."
    )
    tile_coords = [(x, y) for x in range(TILES_PER_SIDE) for y in range(TILES_PER_SIDE)]

    # Step 3: Fetch all tiles concurrently
    print(
        f"🌍 Fetching all zoom {ZOOM_LEVEL} tiles (max {max_concurrent} concurrent)..."
    )
    semaphore = asyncio.Semaphore(max_concurrent)

    tasks = [fetch_tile(client, api_url, x, y, semaphore) for x, y in tile_coords]

    # Show progress as tiles complete
    completed = 0
    for task in asyncio.as_completed(tasks):
        x, y, elapsed, success = await task
        completed += 1

        if success:
            result.tile_times.append(elapsed)
        else:
            result.tile_failures.append((x, y))

        # Show progress every 10% completion
        if completed % (len(tile_coords) // 10) == 0:
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
    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["tile_x", "tile_y", "response_time_s", "success"])

        # Write successful tiles
        tile_coords = [
            (x, y) for x in range(TILES_PER_SIDE) for y in range(TILES_PER_SIDE)
        ]
        tile_idx = 0
        failure_coords = set(result.tile_failures)

        for x, y in tile_coords:
            if (x, y) in failure_coords:
                writer.writerow([x, y, "N/A", False])
            elif tile_idx < len(result.tile_times):
                writer.writerow([x, y, f"{result.tile_times[tile_idx]:.3f}", True])
                tile_idx += 1

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

    args = parser.parse_args()

    # Override with environment variable if set
    api_url = os.environ.get("API_URL", args.api_url)

    print(f"🎯 Benchmarking Lambda at: {api_url}")
    print(f"📊 Dataset: {DATASET_PARAMS['url']}")
    print(f"🔍 Variable: {DATASET_PARAMS['variable']}")
    print(f"⚡ Max concurrent requests: {args.max_concurrent}")
    print()

    # Configure httpx client with appropriate timeouts
    timeout = httpx.Timeout(60.0, connect=10.0)
    limits = httpx.Limits(max_connections=args.max_concurrent * 2)

    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        result = await benchmark_tiles(client, api_url, args.max_concurrent)

        print_summary(result)

        if args.export_csv:
            export_csv(result)


if __name__ == "__main__":
    asyncio.run(main())
