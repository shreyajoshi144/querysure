"""Connector registry. The UI and API list exactly what is registered here and nothing else."""
from __future__ import annotations

import os
from typing import Dict, List, Optional

from connectors.base import Connector
from connectors.database import DatabaseConnector
from connectors.files import CsvConnector, ExcelConnector, JsonConnector, ParquetConnector, SqliteConnector
from connectors.mysql import MysqlConnector
from connectors.postgres import PostgresConnector
from connectors.rest import RestConnector
from connectors.s3_source import S3SourceConnector

_CONNECTORS: Dict[str, Connector] = {c.name: c for c in (CsvConnector(), ExcelConnector(), JsonConnector(), ParquetConnector(), SqliteConnector())}

_DATABASES: Dict[str, DatabaseConnector] = {c.name: c for c in (PostgresConnector(), MysqlConnector())}
# Sprint 5: sources that deliver records (REST) or objects (S3) instead of rows
_RECORD_SOURCES = {c.name: c for c in (RestConnector(), S3SourceConnector())}


def get(name: str) -> Optional[Connector]:
    return _CONNECTORS.get(name)


def database(name: str) -> Optional[DatabaseConnector]:
    return _DATABASES.get(name)


def source(name: str):
    """Any connection connector: database, REST or S3. All offer validate_config, test_connection, discover_schema and summarize."""
    return _DATABASES.get(name) or _RECORD_SOURCES.get(name)


def for_extension(ext: str) -> Optional[Connector]:
    ext = (ext or "").lower()
    return next((c for c in _CONNECTORS.values() if ext in c.extensions), None)


def for_filename(filename: str) -> Optional[Connector]:
    return for_extension(os.path.splitext(filename or "")[1])


def supported_extensions() -> tuple:
    return tuple(ext for c in _CONNECTORS.values() for ext in c.extensions)


def describe() -> List[dict]:
    """Capabilities as reported to clients. Nothing here is aspirational: every entry is implemented."""
    out = []
    for c in _CONNECTORS.values():
        cap = c.capabilities()
        out.append({"name": c.name, "label": c.label, "extensions": list(cap.extensions), "kind": "file",
                    "full_refresh": cap.full_refresh, "incremental": cap.incremental, "requires_credentials": cap.credentials})
    for c in _DATABASES.values():
        cap = c.capabilities()
        out.append({"name": c.name, "label": c.label, "extensions": [], "kind": "database", "full_refresh": cap.full_refresh,
                    "incremental": cap.incremental, "requires_credentials": cap.credentials})
    for c in _RECORD_SOURCES.values():
        cap = c.capabilities()
        out.append({"name": c.name, "label": c.label, "extensions": [], "kind": "api" if c.name == "rest" else "object_storage",
                    "full_refresh": cap.full_refresh, "incremental": cap.incremental, "requires_credentials": cap.credentials})
    return out
