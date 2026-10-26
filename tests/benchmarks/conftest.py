"""Collect benchmarks only when pytest-benchmark is explicitly enabled.

`pytest` alone runs the normal suite; `pytest tests/benchmarks
--benchmark-only` runs these (see README in this directory).
"""

import pytest


def pytest_collection_modifyitems(config, items):
    """Skip benchmark tests unless --benchmark-only / --benchmark-enable."""
    if config.getoption("benchmark_only", False) or config.getoption(
        "benchmark_enable", False
    ):
        return
    skip = pytest.mark.skip(reason="run with --benchmark-only")
    for item in items:
        if "benchmarks" in str(item.fspath):
            item.add_marker(skip)
