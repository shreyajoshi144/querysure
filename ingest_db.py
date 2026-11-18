"""Batch and incremental ingestion from a database connection (Sprint 4).

A dataset imported from a connection maps to ONE source table. Each run reads it in keyset-paginated batches into a copy of the
dataset's SQLite file and publishes it through the same version swap as file imports (ingest.py), so every guarantee carries over:
a failed, interrupted or lost run leaves the previous version untouched, and only one run per dataset is active.

Modes
  full_refresh  rebuild from scratch, ordered by primary key (a table with no key is streamed once and cannot resume mid-run).
                A first import of an incremental dataset is its backfill: a full refresh that also records the checkpoint.
  incremental   rows whose watermark column is >= (stored watermark - overlap), upserted by primary key onto a copy of the current
                version, then the new watermark is stored. Re-reading a window changes nothing (identical rows count as skipped),
                so re-running it can neither duplicate nor lose rows. The checkpoint is written in the swap transaction.
                Incremental needs a primary key, an integer or timestamp watermark column, a checkpoint and an unchanged source
                schema; otherwise the run is a full refresh and records why (``fallback_reason``).
Limits (documented, not hidden): deletions in the source are not detected by incremental runs (a full refresh reconciles them); a
row whose watermark column is NULL is never picked up incrementally.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
import uuid
from datetime import timedelta
from typing import Callable, List, Optional

import config
import ingest
import secrets_store
import quality
import storage
from connectors import registry
from connectors.database import (ConnectorError, SourceTable, UnsupportedValue, format_watermark, parse_watermark, table_by_name,
                                 to_cell)
from db import Connection, Dataset, IngestionCheckpoint, IngestionRun
from modules import ingestion

logger = logging.getLogger("querysure.ingest_db")

MAX_ROW_CODES = 1000
RETRIES = 3


class SourceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _q(ident: str) -> str:
    return '"' + str(ident).replace('"', '""') + '"'


class _Source:
    """One live source connection that reconnects (with bounded backoff) when a transient failure interrupts a batch."""

    def __init__(self, connector, cfg: dict, secrets: dict):
        self.connector, self.cfg, self.secrets = connector, cfg, secrets
        self.conn = None

    def run(self, fn):
        for attempt in range(RETRIES):
            try:
                if self.conn is None:
                    self.conn = self.connector.open(self.cfg, self.secrets)
                return fn(self.conn)
            except ConnectorError as exc:
                self.close()
                if not exc.transient or attempt == RETRIES - 1:
                    raise
                time.sleep(min(2 ** attempt, 5))

    def close(self):
        if self.conn is not None:
            self.connector.close(self.conn)
            self.conn = None


def plan_mode(ds: Optional[Dataset], table: SourceTable, requested: str, wm_col: Optional[str], checkpoint) -> tuple:
    """-> (mode, fallback_reason). An initial load is a full refresh without a fallback reason."""
    if requested != "incremental":
        return "full_refresh", None
    new = ds is None or (ds.version or 0) == 0
    col = table.column(wm_col) if wm_col else None
    if not wm_col:
        return "full_refresh", "no_watermark"
    if col is None or col.kind == "other":
        return "full_refresh", "watermark_type_unsupported"
    if not table.primary_key:
        return "full_refresh", "no_primary_key"
    if new:
        return "full_refresh", None
    if checkpoint is None:
        return "full_refresh", "no_checkpoint"
    if (ds.sync_config or {}).get("source_schema") != table.schema():
        return "full_refresh", "schema_changed"
    if not storage.is_workspace_ref(ds.storage_ref):
        return "full_refresh", "previous_version_unavailable"
    return "incremental", None


def _create_table(con: sqlite3.Connection, name: str, table: SourceTable) -> None:
    cols = ", ".join(f"{_q(c.name)} {c.sqlite_type}" for c in table.columns)
    pk = f", PRIMARY KEY ({', '.join(_q(k) for k in table.primary_key)})" if table.primary_key else ""
    con.execute(f"CREATE TABLE {_q(name)} ({cols}{pk})")
    con.execute("CREATE TABLE IF NOT EXISTS _qs_quarantine (run_id TEXT, table_name TEXT, row_number INTEGER, column_name TEXT, "
                "reason TEXT, value TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS _qs_lineage (table_name TEXT, source_type TEXT, connector TEXT, source_name TEXT, "
                "run_id TEXT, ingested_at TEXT, row_count INTEGER)")


def _apply(con, tbl: str, columns: List[str], pk_idx: List[int], rows: list, upsert: bool) -> tuple:
    """Write converted rows. Returns (inserted, updated, skipped). Upsert compares against the stored row so an identical re-read
    is a no-op."""
    if not rows:
        return 0, 0, 0
    marks = ", ".join("?" * len(columns))
    insert = f"INSERT INTO {_q(tbl)} ({', '.join(_q(c) for c in columns)}) VALUES ({marks})"
    if not upsert:
        con.executemany(insert, rows)
        return len(rows), 0, 0
    existing: dict = {}
    step = max(1, 400 // len(pk_idx))
    cond = "(" + " AND ".join(f"{_q(columns[i])} = ?" for i in pk_idx) + ")"
    for start in range(0, len(rows), step):
        chunk = rows[start:start + step]
        params = [r[i] for r in chunk for i in pk_idx]
        for old in con.execute(f"SELECT {', '.join(_q(c) for c in columns)} FROM {_q(tbl)} WHERE " + " OR ".join([cond] * len(chunk)), params):
            existing[tuple(old[i] for i in pk_idx)] = tuple(old)
    new_rows, changed, skipped = [], [], 0
    for r in rows:
        old = existing.get(tuple(r[i] for i in pk_idx))
        if old is None:
            new_rows.append(r)
        elif tuple(r) != old:
            changed.append(r)
        else:
            skipped += 1
    con.executemany(insert, new_rows)
    others = [i for i in range(len(columns)) if i not in pk_idx]
    if changed and others:
        sets = ", ".join(f"{_q(columns[i])} = ?" for i in others)
        where = " AND ".join(f"{_q(columns[i])} = ?" for i in pk_idx)
        con.executemany(f"UPDATE {_q(tbl)} SET {sets} WHERE {where}",
                        [[r[i] for i in others] + [r[i] for i in pk_idx] for r in changed])
    return len(new_rows), len(changed), skipped


def publish(db, *, ds: Dataset, run: IngestionRun, connection: Connection, tmp: str, curated: str, stats: dict, reasons: dict,
            changes: list, new_dataset: bool, quota_check, abort, wm_col, wm_kind, wm_after, total: int, checkpoint,
            sync_config: dict, state: Optional[dict] = None, table_stats: Optional[dict] = None, table_count: int = 1) -> str:
    """Store the built file and swap it in: dataset, run, checkpoint and sync config change in ONE transaction. On any failure the
    stored object is removed and nothing visible has changed. Returns the new curated reference."""
    new_bytes = os.path.getsize(tmp)
    if quota_check:
        quota_check(new_bytes, ds)
    obj_ws = ds.workspace_id if new_dataset else ingest._object_workspace(ds)
    curated_ref = storage.store_workspace_object(tmp, obj_ws, ds.id, f"curated-{run.id}.db", cache=True)
    try:
        abort()
        ds.storage_ref, ds.status, ds.connector, ds.fingerprint = curated_ref, "ready", connection.connector, None
        ds.bytes, ds.table_count, ds.row_count = new_bytes, table_count, total
        ds.version = (ds.version or 0) + 1
        if changes and not new_dataset:
            ds.schema_version = (ds.schema_version or 1) + 1
        ds.sync_config = sync_config
        if wm_after is not None and wm_col:
            if checkpoint is None:
                db.add(IngestionCheckpoint(dataset_id=ds.id, column_name=wm_col, column_type=wm_kind, value=wm_after,
                                           state=state, run_id=run.id))
            else:
                checkpoint.column_name, checkpoint.column_type, checkpoint.value, checkpoint.run_id = wm_col, wm_kind, wm_after, run.id
                if state is not None:
                    checkpoint.state = state
        run.status, run.finished_at, run.dataset_version = "succeeded", ingest._now(), ds.version
        run.records_read, run.records_inserted, run.records_updated = stats["read"], stats["inserted"], stats["updated"]
        run.records_skipped, run.records_rejected = stats["skipped"], stats["rejected"]
        run.reject_reasons, run.schema_changes = reasons, changes
        run.table_stats = table_stats or {curated: {k: stats[k] for k in ("read", "inserted", "updated", "skipped", "rejected")}}
        run.watermark_after, run.curated_ref = wm_after, curated_ref
        db.commit()
    except BaseException:
        db.rollback()
        storage.delete_workspace_object(curated_ref)
        raise
    return curated_ref


def sync(db, *, user, workspace_id: str, connection: Connection, table_name: str, requested_mode: str,
         dataset: Optional[Dataset] = None, watermark_column: Optional[str] = None, name: Optional[str] = None,
         job_id: Optional[str] = None, abort: Callable[[], None] = lambda: None,
         quota_check: Optional[Callable[[int, Dataset], None]] = None) -> ingest.ImportResult:
    """Run one import. ``dataset`` None creates a new dataset for ``table_name``."""
    connector = registry.database(connection.connector)
    if connector is None:
        raise SourceError("UNSUPPORTED_CONNECTOR", "This connection type is not supported.")
    if connection.status != "active":
        raise SourceError("CONNECTION_DISABLED", "This connection is disabled.")
    try:
        secrets = secrets_store.decrypt(connection.id, connection.secret_ciphertext) if connection.secret_ciphertext else {}
    except secrets_store.SecretsUnavailable:
        raise SourceError("SECRETS_UNAVAILABLE", "The saved connection secret cannot be used; edit the connection and enter the password again.") from None
    new_dataset = dataset is None
    if new_dataset:
        label = (name or f"{connection.name}.{table_name}")[:255]
        dataset = Dataset(workspace_id=workspace_id, created_by=user.id, name=label, source_type="database",
                          file_format=connection.connector, storage_ref=f"pending:{uuid.uuid4()}", status="importing", version=0,
                          connector=connection.connector, connection_id=connection.id, source_table=table_name)
        db.add(dataset)
        db.commit()
    ds = dataset
    try:
        run = ingest.start_run(db, ds, user_id=user.id, job_id=job_id, connector=connection.connector,
                               source_name=f"{connection.name}.{table_name}", source_bytes=0, fp="")
    except BaseException:
        if new_dataset:
            ingest.discard_dataset_row(db, ds)
        raise
    cfg = connection.config
    src = _Source(connector, cfg, secrets)
    tmp = os.path.join(storage.UPLOAD_DIR, f".build-{uuid.uuid4().hex}.db")
    curated_ref = None
    try:
        os.makedirs(storage.UPLOAD_DIR, exist_ok=True)
        tables = src.run(lambda c: connector.tables(c, cfg))
        table = table_by_name(tables, table_name)
        if table is None:
            raise SourceError("TABLE_NOT_FOUND", "The table no longer exists or is not visible to this connection's user.")
        sc = dict(ds.sync_config or {})
        wm_col = watermark_column or sc.get("watermark_column")
        checkpoint = None if new_dataset else db.query(IngestionCheckpoint).filter(IngestionCheckpoint.dataset_id == ds.id).first()
        mode, fallback = plan_mode(None if new_dataset else ds, table, requested_mode, wm_col, checkpoint)
        incremental = mode == "incremental"
        wm_kind = table.column(wm_col).kind if (wm_col and table.column(wm_col)) else None
        curated = sc.get("curated_table") or ingestion.safe_table_name(table_name)
        run.mode, run.fallback_reason = mode, fallback
        run.watermark_before = checkpoint.value if checkpoint else None
        db.commit()

        prev_ref = ds.storage_ref
        prev_schema = {}
        if incremental:
            shutil.copyfile(storage.ensure_local_workspace(prev_ref), tmp)
        elif not new_dataset and storage.is_workspace_ref(prev_ref):
            try:
                prev_schema = ingest.read_schema(storage.ensure_local_workspace(prev_ref))
            except Exception:  # noqa: BLE001 - an unreadable previous version only costs the comparison
                prev_schema = {}
        con = sqlite3.connect(tmp)
        columns = [c.name for c in table.columns]
        pk_idx = [columns.index(k) for k in table.primary_key]
        stats = {"read": 0, "inserted": 0, "updated": 0, "skipped": 0, "rejected": 0}
        reasons: dict = {}
        quarantine: list = []
        max_wm = None
        wm_idx = columns.index(wm_col) if wm_col in columns else None
        try:
            if not incremental:
                _create_table(con, curated, table)
            batch = config.CONNECTION_FETCH_BATCH_ROWS
            deadline = time.monotonic() + config.CONNECTION_SYNC_MAX_SECONDS
            start = None
            if incremental:
                start = parse_watermark(wm_kind, checkpoint.value)
                if wm_kind == "timestamp":
                    start = start - timedelta(seconds=config.INCREMENTAL_OVERLAP_SECONDS)
            order = ([wm_col] + [k for k in table.primary_key if k != wm_col]) if incremental else list(table.primary_key)

            def handle(rows: list) -> None:
                nonlocal max_wm
                good = []
                for raw in rows:
                    stats["read"] += 1
                    try:
                        cells = [to_cell(v) for v in raw]
                    except UnsupportedValue as exc:
                        stats["rejected"] += 1
                        reasons[exc.reason] = reasons.get(exc.reason, 0) + 1
                        if len(quarantine) < MAX_ROW_CODES:
                            quarantine.append((run.id, curated, stats["read"], None, exc.reason, None))
                        continue
                    good.append(cells)
                    if wm_idx is not None and cells[wm_idx] is not None:
                        v = parse_watermark(wm_kind, str(cells[wm_idx])) if wm_kind else None
                        if v is not None and (max_wm is None or v > max_wm):
                            max_wm = v
                ins, upd, skip = _apply(con, curated, columns, pk_idx, good, upsert=incremental)
                stats["inserted"] += ins
                stats["updated"] += upd
                stats["skipped"] += skip
                if stats["read"] > ingestion.MAX_INGEST_ROWS:
                    raise ValueError(f"The source table has more than {ingestion.MAX_INGEST_ROWS} rows.")
                run.records_read, run.records_inserted, run.records_updated = stats["read"], stats["inserted"], stats["updated"]
                run.records_skipped, run.records_rejected = stats["skipped"], stats["rejected"]
                db.commit()

            if table.primary_key:
                after = None
                while True:
                    abort()
                    if time.monotonic() > deadline:
                        raise SourceError("SYNC_TIMEOUT", "The sync took longer than the allowed time and was stopped.")
                    rows = src.run(lambda c: connector.read_keyset(
                        c, cfg, table, order, after, batch, watermark=(wm_col, start) if incremental else None))
                    if not rows:
                        break
                    handle(rows)
                    last = rows[-1]
                    after = tuple(last[columns.index(o)] for o in order)
                    if len(rows) < batch:
                        break
            else:
                src.run(lambda c: c)    # open (with retries); a stream cannot resume, so a mid-stream failure fails the run
                for rows in connector.stream_all(src.conn, cfg, table, batch):
                    abort()
                    if time.monotonic() > deadline:
                        raise SourceError("SYNC_TIMEOUT", "The sync took longer than the allowed time and was stopped.")
                    handle(rows)
            con.execute("DELETE FROM _qs_lineage WHERE table_name = ?", (curated,))
            con.execute("INSERT INTO _qs_lineage VALUES (?,?,?,?,?,?,?)",
                        (curated, "database", connection.connector, f"{connection.name}.{table_name}", run.id,
                         ingest._now().isoformat(timespec="seconds"), stats["inserted"] + stats["updated"] + stats["skipped"]))
            con.executemany("INSERT INTO _qs_quarantine VALUES (?,?,?,?,?,?)", quarantine)
            total = con.execute(f"SELECT COUNT(*) FROM {_q(curated)}").fetchone()[0]
            con.commit()
        finally:
            con.close()
        abort()
        changes = ingest.diff_schema(prev_schema, ingest.read_schema(tmp)) if prev_schema else []
        wm_after = None
        if wm_kind and wm_col:
            cur = parse_watermark(wm_kind, checkpoint.value) if (checkpoint and incremental) else None
            best = max_wm if cur is None or (max_wm is not None and max_wm > cur) else cur
            if best is not None:
                wm_after = format_watermark(wm_kind, best)
            elif checkpoint:
                wm_after = checkpoint.value
        curated_ref = publish(
            db, ds=ds, run=run, connection=connection, tmp=tmp, curated=curated, stats=stats, reasons=reasons, changes=changes,
            new_dataset=new_dataset, quota_check=quota_check, abort=abort, wm_col=wm_col, wm_kind=wm_kind, wm_after=wm_after,
            total=total, checkpoint=checkpoint,
            sync_config={"mode": requested_mode, "watermark_column": wm_col, "watermark_type": wm_kind,
                         "primary_key": list(table.primary_key), "curated_table": curated, "source_schema": table.schema()})
    except BaseException as exc:
        db.rollback()
        storage.delete_workspace_object(curated_ref)
        ingest._fail(db, run, ds, exc, new_dataset)
        raise
    finally:
        src.close()
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    if not new_dataset and prev_ref != curated_ref:
        storage.delete_workspace_object(prev_ref)
    db.refresh(ds)
    db.refresh(run)
    quality.record(db, ds, run)
    return ingest.ImportResult(ds, run)
