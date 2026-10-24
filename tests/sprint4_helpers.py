"""Shared helpers for Sprint 4 tests (kept out of conftest.py so existing fixtures stay untouched)."""
from __future__ import annotations

import io
import json
import logging

import pytest

from observability import metrics as obs_metrics
from observability.logging_setup import JsonFormatter
from observability.store import MetricsStore, get_store, set_store


@pytest.fixture()
def fresh_store():
    """A clean in-process metrics store installed as the process-wide store for one test."""
    store = MetricsStore()
    set_store(store)
    yield store
    set_store(None)


@pytest.fixture()
def log_lines():
    """Capture everything logged during a test through the REAL JSON formatter. Returns (parsed_lines, raw_text)."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)

    def read():
        # The httpx logger is the TEST CLIENT logging its own outgoing request URL; it is not server output.
        kept = [line for line in buf.getvalue().splitlines() if line.strip() and '"logger":"httpx"' not in line]
        return [json.loads(line) for line in kept], "\n".join(kept)
    yield read
    root.removeHandler(handler)
    root.setLevel(old_level)


__all__ = ["fresh_store", "log_lines", "obs_metrics", "get_store"]
