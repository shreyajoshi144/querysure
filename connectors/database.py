"""Database source connectors (Sprint 4): shared logic for PostgreSQL and MySQL.

Reading is keyset-paginated: every batch is one short ``SELECT ... WHERE (key) > (last) ORDER BY key LIMIT n`` query, so a batch
is stateless, memory is bounded, and a run can reconnect after a transient failure and continue exactly where it stopped.
Identifiers are only ever taken from the discovered catalog (never from a request) and are quoted; values are bound parameters.
Connections are read-only, time-limited, and refuse non-public addresses (connectors/netguard.py). Driver messages are never
surfaced: errors map to a small set of codes.
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import config
from connectors import netguard
from connectors.base import Capabilities, ConnectionResult

MAX_TABLES = 500
MAX_TEXT_CELL = 1_000_000
SSL_MODES = ("disable", "require", "verify-full")

_INT_TYPES = {"smallint", "integer", "int", "bigint", "serial", "bigserial", "tinyint", "mediumint", "int2", "int4", "int8"}
_BOOL_TYPES = {"boolean", "bool", "bit", "year"}
_REAL_TYPES = {"real", "double precision", "double", "float", "float4", "float8", "numeric", "decimal", "money"}
_TS_TYPES = {"timestamp", "timestamp without time zone", "timestamp with time zone", "timestamptz", "datetime"}


class ConnectorError(ValueError):   # a ValueError so a job reports it once instead of retrying it a second time
    """A source failure reduced to a code the UI and run history can show. ``transient`` failures are retried with backoff."""

    def __init__(self, code: str, message: str, transient: bool = False):
        super().__init__(message)
        self.code, self.message, self.transient = code, message, transient


class UnsupportedValue(ValueError):
    """A cell that cannot be stored; the row is quarantined with ``reason``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class SourceColumn:
    name: str
    type: str
    sqlite_type: str
    kind: str       # integer | timestamp | other  (the kinds usable as an incremental watermark are integer and timestamp)


@dataclass
class SourceTable:
    name: str
    columns: List[SourceColumn] = field(default_factory=list)
    primary_key: List[str] = field(default_factory=list)
    is_view: bool = False
    # Sprint 5: REST and S3 resources decide their own incremental readiness and watermark (None = derive it from keys and columns)
    incremental_ready: Optional[bool] = None
    watermark: Optional[str] = None

    def column(self, name: str) -> Optional[SourceColumn]:
        return next((c for c in self.columns if c.name == name), None)

    def schema(self) -> Dict[str, str]:
        return {c.name: c.sqlite_type for c in self.columns}


def describe_type(source_type: str) -> Tuple[str, str]:
    """(sqlite declared type, watermark kind) for a source type name."""
    t = (source_type or "").lower().strip()
    if t in _BOOL_TYPES:
        return "INTEGER", "other"
    if t in _INT_TYPES:
        return "INTEGER", "integer"
    if t in _REAL_TYPES:
        return "REAL", "other"
    if t in _TS_TYPES:
        return "TIMESTAMP", "timestamp"
    if t == "date":
        return "DATE", "timestamp"
    return "TEXT", "other"


def to_cell(v: Any) -> Any:
    """Convert one source value to something SQLite can store, or raise UnsupportedValue."""
    if v is None or isinstance(v, (str, int)) and not isinstance(v, bool):
        if isinstance(v, str) and len(v) > MAX_TEXT_CELL:
            raise UnsupportedValue("value_too_large")
        return v
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, Decimal):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v.isoformat(sep=" ")
    if isinstance(v, (date, dtime)):
        return v.isoformat()
    if isinstance(v, timedelta):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        if len(v) > MAX_TEXT_CELL // 2:
            raise UnsupportedValue("value_too_large")
        return bytes(v).hex()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (dict, list, tuple)):
        text = json.dumps(v, default=str)
        if len(text) > MAX_TEXT_CELL:
            raise UnsupportedValue("value_too_large")
        return text
    return str(v)


def parse_watermark(kind: str, text: str):
    """Stored/cell text -> a value to bind in a query."""
    if kind == "integer":
        return int(text)
    if kind == "timestamp":
        value = datetime.fromisoformat(str(text).replace("T", " ").strip().replace("Z", "+00:00"))
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    raise ValueError("unsupported watermark type")


def format_watermark(kind: str, value) -> str:
    if kind == "integer":
        return str(int(value))
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day).isoformat(sep=" ")
    return datetime.fromisoformat(str(value)).isoformat(sep=" ")


class DatabaseConnector:
    name = ""
    label = ""
    pipeline = "keyset"
    credential_keys = ("host", "port", "user")
    default_port = 0
    quote_char = '"'

    # ---- interface shared with the file connectors
    def capabilities(self) -> Capabilities:
        return Capabilities(full_refresh=True, incremental=True, extensions=(), credentials=True)

    def validate_config(self, cfg: Optional[dict]) -> dict:
        """Normalize non-secret settings; raises ValueError with a user-actionable message."""
        cfg = dict(cfg or {})
        unknown = set(cfg) - {"host", "port", "database", "user", "ssl_mode", "schema"}
        if unknown:
            raise ValueError(f"Unknown connection setting(s): {', '.join(sorted(unknown))}.")
        out = {
            "host": netguard.validate_host(cfg.get("host")),
            "port": netguard.validate_port(cfg.get("port", self.default_port)),
            "database": self._text(cfg.get("database"), "Database", 128),
            "user": self._text(cfg.get("user"), "User", 128),
            "ssl_mode": cfg.get("ssl_mode", "require"),
        }
        if out["ssl_mode"] not in SSL_MODES:
            raise ValueError(f"ssl_mode must be one of: {', '.join(SSL_MODES)}.")
        if out["ssl_mode"] == "disable" and config.IS_PRODUCTION:
            raise ValueError("Unencrypted connections are not allowed in production; use ssl_mode require or verify-full.")
        schema = cfg.get("schema")
        if schema:
            out["schema"] = self._text(schema, "Schema", 128)
        return out

    @staticmethod
    def _text(value, label: str, limit: int) -> str:
        value = (value or "").strip() if isinstance(value, str) else ""
        if not value or len(value) > limit or any(ord(ch) < 32 for ch in value):
            raise ValueError(f"{label} is required (at most {limit} characters).")
        return value

    def summarize(self, cfg: dict) -> str:
        return f"{cfg.get('user')}@{cfg.get('host')}:{cfg.get('port')}/{cfg.get('database')}"

    def test_connection(self, cfg: dict, secrets: dict) -> ConnectionResult:
        conn = self.open(cfg, secrets)
        try:
            return ConnectionResult(True, f"Connected ({self.server_version(conn)}).")
        finally:
            self.close(conn)

    def discover_schema(self, cfg: dict, secrets: dict) -> List[SourceTable]:
        conn = self.open(cfg, secrets)
        try:
            return self.tables(conn, cfg)
        finally:
            self.close(conn)

    # ---- driver hooks
    def open(self, cfg: dict, secrets: dict):
        raise NotImplementedError

    def close(self, conn) -> None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    def tables(self, conn, cfg: dict) -> List[SourceTable]:
        raise NotImplementedError

    def server_version(self, conn) -> str:
        return "ok"

    def classify(self, exc: BaseException) -> ConnectorError:
        raise NotImplementedError

    def _fetch(self, conn, sql: str, params: Sequence = ()) -> list:
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql, tuple(params))
                return list(cur.fetchall())
            finally:
                cur.close()
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001 - mapped to a code, never surfaced raw
            raise self.classify(exc) from None

    def _stream(self, conn, sql: str, batch: int) -> Iterator[list]:
        raise NotImplementedError

    # ---- reading
    def q(self, ident: str) -> str:
        return self.quote_char + str(ident).replace(self.quote_char, self.quote_char * 2) + self.quote_char

    def qualified(self, cfg: dict, table: SourceTable) -> str:
        return self.q(table.name)

    def read_keyset(self, conn, cfg: dict, table: SourceTable, order: List[str], after: Optional[Sequence],
                    limit: int, watermark: Optional[Tuple[str, Any]] = None) -> list:
        """One batch ordered by ``order`` (every name must be a discovered column), strictly after the key tuple ``after``;
        with ``watermark=(column, start)`` only rows whose column is >= start."""
        known = {c.name for c in table.columns}
        cols = [c.name for c in table.columns]
        if not order or any(o not in known for o in order) or (watermark and watermark[0] not in known):
            raise ConnectorError("SCHEMA_CHANGED", "The source table no longer has the expected columns.")
        conds, params = [], []
        if watermark:
            conds.append(f"{self.q(watermark[0])} >= %s")
            params.append(watermark[1])
        if after is not None:
            conds.append(f"({', '.join(self.q(o) for o in order)}) > ({', '.join(['%s'] * len(order))})")
            params.extend(after)
        sql = (f"SELECT {', '.join(self.q(c) for c in cols)} FROM {self.qualified(cfg, table)}"
               + (f" WHERE {' AND '.join(conds)}" if conds else "")
               + f" ORDER BY {', '.join(self.q(o) for o in order)} LIMIT {int(limit)}")
        return self._fetch(conn, sql, params)

    def stream_all(self, conn, cfg: dict, table: SourceTable, batch: int) -> Iterator[list]:
        """Whole-table streaming for a table without a primary key (full refresh only; cannot resume mid-way)."""
        sql = f"SELECT {', '.join(self.q(c.name) for c in table.columns)} FROM {self.qualified(cfg, table)}"
        try:
            yield from self._stream(conn, sql, batch)
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self.classify(exc) from None


def table_by_name(tables: List[SourceTable], name: str) -> Optional[SourceTable]:
    return next((t for t in tables if t.name == name), None)
