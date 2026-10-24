"""Loading the shipped (or a user-supplied) benchmark file."""
from __future__ import annotations

import os
from typing import Optional

from .models import Benchmark, DatasetError, parse_benchmark

DEFAULT_BENCHMARK_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "datasets", "retail_benchmark.json"
)


def load_benchmark(path: Optional[str] = None) -> Benchmark:
    path = path or DEFAULT_BENCHMARK_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as e:
        raise DatasetError(f"cannot read benchmark file {path}: {e}") from e
    return parse_benchmark(raw, path=path)
