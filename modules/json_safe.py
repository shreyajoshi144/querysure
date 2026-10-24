"""Make values read from a user's database safe to persist in JSON columns.

Why this exists
---------------
Sample rows (schema profiling) and query results are stored in the session
row's JSON columns. Raw database/pandas values break that in three ways:

  * BLOB columns come back as ``bytes`` -> ``json.dumps`` raises TypeError,
    which surfaced as a 500 on upload (profiling) or on /api/execute-sql.
  * NULLs in numeric columns become ``NaN`` once they pass through pandas, and
    SQLite can hold ``Infinity``. Python's json writes the bare tokens ``NaN``
    / ``Infinity``, which are NOT valid JSON: SQLite stores them happily
    (so dev and the test-suite never notice) but PostgreSQL's json/jsonb
    types reject them, so the same upload fails only in production.
  * Timestamps / Decimals / numpy scalars are not JSON serializable either.

Everything is mapped to a plain JSON scalar; NaN/Infinity become None (the
honest "no value"), and binary data becomes a short descriptive placeholder.
"""
from __future__ import annotations

import datetime
import decimal
import math
from typing import Any, Dict, Iterable, List


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary {len(bytes(value))} bytes>"
    if isinstance(value, decimal.Decimal):
        return float(value) if value.is_finite() else None
    # NaN-likes (pandas NaT / NA, numpy nan) are the only values not equal to
    # themselves. Checked before isoformat() because NaT has an isoformat that
    # returns the string "NaT".
    try:
        if value != value:
            return None
    except Exception:
        pass
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    item = getattr(value, "item", None)  # numpy scalar -> python scalar
    if callable(item):
        try:
            return json_safe(item())
        except Exception:
            pass
    return str(value)


def json_safe_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{k: json_safe(v) for k, v in row.items()} for row in rows]
