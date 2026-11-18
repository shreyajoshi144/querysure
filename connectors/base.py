"""Connector interface (Sprint 3).

A connector knows how to reach one kind of source and turn it into tables of rows. Everything after that -- validation,
quarantine, the curated database, lineage, versioning -- is the ingestion pipeline's job (ingest.py), shared by every connector.

File connectors read a whole file that the upload layer already capped (MAX_UPLOAD_MB), so ``read_tables`` returns each table
as one normalized DataFrame and ``read_batches`` slices it; they are full-refresh only and need no credentials. Connectors
that reach a live system (Sprint 4) will implement ``read_batches`` natively with a cursor and declare credentials.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

import pandas as pd

DEFAULT_BATCH_ROWS = 5000
MAX_BATCH_ROWS = 50000


@dataclass(frozen=True)
class Capabilities:
    full_refresh: bool = True
    incremental: bool = False          # no file connector supports it; the UI never offers it
    extensions: Tuple[str, ...] = ()
    credentials: bool = False          # reserved: nothing stores a secret before Sprint 4


@dataclass
class ConnectionResult:
    ok: bool
    message: str = ""


@dataclass
class Rejects:
    """Rows a connector could not turn into records (e.g. a CSV line with more fields than the header)."""
    rows: List[dict] = field(default_factory=list)


class Connector(ABC):
    name: str = ""
    label: str = ""
    extensions: Tuple[str, ...] = ()
    passthrough: bool = False          # True: the source already IS a SQLite database (copied, not re-tabulated)

    def capabilities(self) -> Capabilities:
        return Capabilities(extensions=self.extensions)

    def validate_config(self, config: Optional[dict]) -> dict:
        """Normalize and validate connector settings; raises ValueError on a bad value."""
        config = dict(config or {})
        batch = config.get("batch_size", DEFAULT_BATCH_ROWS)
        if not isinstance(batch, int) or isinstance(batch, bool) or not 1 <= batch <= MAX_BATCH_ROWS:
            raise ValueError(f"batch_size must be an integer between 1 and {MAX_BATCH_ROWS}.")
        unknown = set(config) - {"batch_size"}
        if unknown:
            raise ValueError(f"Unknown connector setting(s): {', '.join(sorted(unknown))}.")
        return {"batch_size": batch}

    def test_connection(self, path: str) -> ConnectionResult:
        try:
            with open(path, "rb") as fh:
                fh.read(1)
        except OSError:
            return ConnectionResult(False, "The source file cannot be read.")
        return ConnectionResult(True, "The source file is readable.")

    @abstractmethod
    def read_tables(self, path: str, base_name: str, rejects: Rejects) -> Dict[str, pd.DataFrame]:
        """Every table in the source, normalized exactly as the upload pipeline always did. Raises ValueError with a
        user-actionable message for a file that cannot be read."""

    def discover_schema(self, path: str, base_name: str) -> Dict[str, Dict[str, str]]:
        return {t: {str(c): str(d) for c, d in df.dtypes.items()} for t, df in self.read_tables(path, base_name, Rejects()).items()}

    def read_batches(self, path: str, base_name: str, batch_size: int = DEFAULT_BATCH_ROWS, cursor: Optional[str] = None,
                     rejects: Optional[Rejects] = None) -> Iterator[Tuple[str, pd.DataFrame]]:
        """(table, batch) pairs. ``cursor`` is for incremental connectors; file connectors are full-refresh and refuse one."""
        if cursor is not None:
            raise ValueError(f"The {self.name} connector is full-refresh only and does not accept a cursor.")
        for table, df in self.read_tables(path, base_name, rejects or Rejects()).items():
            for start in range(0, max(len(df), 1), batch_size):
                yield table, df.iloc[start:start + batch_size]
