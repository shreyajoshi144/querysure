"""Shared helpers for sources that deliver JSON-like records (REST, Sprint 5): flattening, column typing and path lookup."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional

from connectors.database import SourceColumn, describe_type

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
MAX_DEPTH = 2
MAX_COLUMNS = 200


def get_path(obj: Any, path: str):
    """Value at a dotted path ("data.items"); "" returns the object itself. Missing -> None."""
    cur = obj
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def flatten(record: dict, prefix: str = "", depth: int = 0) -> Dict[str, Any]:
    """One level of structure per step, `parent_child` names up to MAX_DEPTH; deeper values and lists become JSON text."""
    out: Dict[str, Any] = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and depth < MAX_DEPTH:
            out.update(flatten(value, name + "_", depth + 1))
        elif isinstance(value, (dict, list)):
            out[name] = json.dumps(value, default=str)
        else:
            out[name] = value
    return out


def sql_type(values: Iterable[Any]) -> str:
    """Declared SQLite type from sample values: all-int INTEGER, numeric REAL, otherwise TEXT."""
    kinds = set()
    for v in values:
        if v is None:
            continue
        kinds.add("int" if isinstance(v, (bool, int)) else "float" if isinstance(v, float) else "text")
    if not kinds:
        return "TEXT"
    if kinds == {"int"}:
        return "INTEGER"
    if kinds <= {"int", "float"}:
        return "REAL"
    return "TEXT"


def infer_columns(records: List[dict], timestamp_fields: Optional[set] = None) -> List[SourceColumn]:
    """Columns in first-seen order from flattened sample records."""
    flat = [flatten(r) for r in records if isinstance(r, dict)]
    names: List[str] = []
    for r in flat:
        for k in r:
            if k not in names:
                names.append(k)
    cols = []
    for name in names[:MAX_COLUMNS]:
        declared = sql_type(r.get(name) for r in flat)
        kind = "integer" if declared == "INTEGER" else "other"
        if timestamp_fields and name in timestamp_fields and declared == "TEXT":
            declared, kind = "TIMESTAMP", "timestamp"
        elif declared == "INTEGER":
            kind = "integer"
        cols.append(SourceColumn(name, declared.lower(), declared if declared != "TIMESTAMP" else "TIMESTAMP", kind))
    return cols


__all__ = ["NAME_RE", "describe_type", "flatten", "get_path", "infer_columns", "sql_type"]
