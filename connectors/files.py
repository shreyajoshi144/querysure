"""File connectors: CSV, Excel, JSON, Parquet and SQLite. They reuse the readers in modules/ingestion.py unchanged (those
carry the format-specific normalization the accuracy evaluation depends on); only Parquet is new."""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Dict

import numpy as np
import pandas as pd

from connectors.base import Connector, Rejects
from modules import ingestion
from modules.data_normalizer import normalize_tabular_values

# A 10 MB Parquet file can hold far more data than a 10 MB CSV (columnar compression), so its decoded size is bounded too.
PARQUET_MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024


def _scalar(v):
    if isinstance(v, np.ndarray):
        v = v.tolist()
    if isinstance(v, (list, dict, tuple)):
        return json.dumps(v, default=str)
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).hex()
    return v


class CsvConnector(Connector):
    name, label, extensions = "csv", "CSV file", (".csv",)

    def read_tables(self, path, base_name, rejects: Rejects) -> Dict[str, pd.DataFrame]:
        bad: list = []
        df = ingestion.read_csv(path, rejects=bad)
        table = ingestion.safe_table_name(base_name)
        rejects.rows.extend({**r, "table": table} for r in bad)
        return {table: df}


class JsonConnector(Connector):
    name, label, extensions = "json", "JSON file", (".json",)

    def read_tables(self, path, base_name, rejects: Rejects) -> Dict[str, pd.DataFrame]:
        return {ingestion.safe_table_name(base_name): ingestion.read_json(path)}


class ExcelConnector(Connector):
    name, label, extensions = "excel", "Excel workbook", (".xlsx", ".xlsm")

    def read_tables(self, path, base_name, rejects: Rejects) -> Dict[str, pd.DataFrame]:
        sheets = ingestion.read_excel(path)
        base = ingestion.safe_table_name(base_name)
        out: Dict[str, pd.DataFrame] = {}
        for sheet_name, df in sheets:
            if len(sheets) == 1:
                name = base
            else:  # same naming and collision rules ingest_to_sqlite always used
                base_part, sheet_part = base[:30], ingestion.safe_table_name(sheet_name, fallback="sheet")[:20]
                name, suffix = f"{base_part}_{sheet_part}"[:60], 2
                while name in out:
                    name = f"{base_part}_{sheet_part}_{suffix}"[:60]
                    suffix += 1
            out[name] = df
        return out


class ParquetConnector(Connector):
    name, label, extensions = "parquet", "Parquet file", (".parquet",)

    def read_tables(self, path, base_name, rejects: Rejects) -> Dict[str, pd.DataFrame]:
        try:
            import pyarrow.parquet as pq   # imported lazily: it is only paid for when a Parquet file is read
            pf = pq.ParquetFile(path)
            meta = pf.metadata
            if meta.num_rows > ingestion.MAX_INGEST_ROWS:
                raise ValueError(f"Parquet file has more than {ingestion.MAX_INGEST_ROWS} rows.")
            if meta.num_columns > ingestion.MAX_INGEST_COLUMNS:
                raise ValueError(f"Parquet file has more than {ingestion.MAX_INGEST_COLUMNS} columns.")
            decoded = sum(meta.row_group(i).total_byte_size for i in range(meta.num_row_groups))
            if decoded > PARQUET_MAX_UNCOMPRESSED_BYTES:
                raise ValueError(f"Parquet file expands to more than {PARQUET_MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB once decoded.")
            df = pf.read().to_pandas()
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - pyarrow raises its own error types for corrupt files
            raise ValueError(f"Parquet parsing failed: {type(exc).__name__}") from exc
        if df.shape[1] == 0:
            raise ValueError("Parquet file contains no columns.")
        if not isinstance(df.index, pd.RangeIndex):
            df = df.reset_index()           # a saved pandas index is data, not metadata
        for col in df.columns:
            if df[col].dtype == object:     # SQLite cells are scalars: nested values become JSON text, decimals floats, bytes hex
                df[col] = df[col].map(_scalar)
        return {ingestion.safe_table_name(base_name): normalize_tabular_values(ingestion.normalize_columns(df, percent_suffix=True))}


class SqliteConnector(Connector):
    name, label, extensions = "sqlite", "SQLite database", (".db", ".sqlite", ".sqlite3")
    passthrough = True

    def read_tables(self, path, base_name, rejects: Rejects) -> Dict[str, pd.DataFrame]:
        raise NotImplementedError("A SQLite source is copied as-is; it has no row reader.")

    def discover_schema(self, path, base_name):
        import sqlite3
        ingestion.validate_sqlite(path)
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            return {t: {c[1]: c[2] for c in con.execute(f'PRAGMA table_info("{t.replace(chr(34), chr(34) * 2)}")')} for t in tables}
        finally:
            con.close()
