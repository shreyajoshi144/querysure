"""Ingestion from REST APIs and S3 objects (Sprint 5). Same guarantees as ingest_db.py (one active run, build aside, one-transaction
swap with the checkpoint, previous version untouched on failure); only the extraction differs.

REST    pages of JSON records are written by primary key (an upsert, so a re-read page changes nothing). Incremental sync sends the
        stored watermark (minus an overlap) as the resource's ``updated_since`` parameter. Fields that appear later become new
        columns and are reported as schema changes.
S3      each object's rows are tagged with ``source_object`` / ``source_modified``. Incremental sync lists the prefix and compares
        ETag, LastModified and size with the previous run: new or changed objects are downloaded and replace their rows, objects
        that vanished have their rows removed, unchanged objects are not touched (their rows count as skipped). A run in which
        nothing changed is recorded as ``skipped`` and creates no new version. If a changed object has a different set of columns,
        the run becomes a full refresh and records why.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
import uuid
from datetime import timedelta
from typing import Callable, Optional

import config
import ingest
import secrets_store
import quality
import storage
from connectors import registry
from connectors.database import UnsupportedValue, format_watermark, parse_watermark, to_cell
from connectors.records import flatten, sql_type
from db import Connection, Dataset, IngestionCheckpoint
from ingest_db import SourceError, _apply, _q, publish
from modules import ingestion

MAX_ROW_CODES = 1000
SOURCE_TYPES = {"rest": "api", "s3": "object_storage"}


class _Fallback(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _columns(con, table: str) -> list:
    return [r[1] for r in con.execute(f"PRAGMA table_info({_q(table)})")]


def _meta_tables(con) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS _qs_quarantine (run_id TEXT, table_name TEXT, row_number INTEGER, column_name TEXT, "
                "reason TEXT, value TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS _qs_lineage (table_name TEXT, source_type TEXT, connector TEXT, source_name TEXT, "
                "run_id TEXT, ingested_at TEXT, row_count INTEGER)")


def sync(db, *, user, workspace_id: str, connection: Connection, table_name: str, requested_mode: str,
         dataset: Optional[Dataset] = None, watermark_column: Optional[str] = None, name: Optional[str] = None,
         job_id: Optional[str] = None, abort: Callable[[], None] = lambda: None,
         quota_check: Optional[Callable[[int, Dataset], None]] = None) -> ingest.ImportResult:
    connector = registry.source(connection.connector)
    if connector is None or connector.pipeline not in ("records", "objects"):
        raise SourceError("UNSUPPORTED_CONNECTOR", "This connection type is not supported.")
    if connection.status != "active":
        raise SourceError("CONNECTION_DISABLED", "This connection is disabled.")
    try:
        secrets = secrets_store.decrypt(connection.id, connection.secret_ciphertext) if connection.secret_ciphertext else {}
    except secrets_store.SecretsUnavailable:
        raise SourceError("SECRETS_UNAVAILABLE", "The saved connection secret cannot be used; edit the connection and enter it again.") from None
    cfg = connection.config
    resource = connector.resource(cfg, table_name)
    if resource is None:
        raise SourceError("TABLE_NOT_FOUND", "That resource is not defined on this connection.")
    new_dataset = dataset is None
    if new_dataset:
        dataset = Dataset(workspace_id=workspace_id, created_by=user.id, name=(name or f"{connection.name}.{table_name}")[:255],
                          source_type=SOURCE_TYPES[connection.connector], file_format=connection.connector,
                          storage_ref=f"pending:{uuid.uuid4()}", status="importing", version=0, connector=connection.connector,
                          connection_id=connection.id, source_table=table_name)
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
    tmp = os.path.join(storage.UPLOAD_DIR, f".build-{uuid.uuid4().hex}.db")
    try:
        os.makedirs(storage.UPLOAD_DIR, exist_ok=True)
        sc = dict(ds.sync_config or {})
        curated = sc.get("curated_table") or ingestion.safe_table_name(table_name)
        checkpoint = None if new_dataset else db.query(IngestionCheckpoint).filter(IngestionCheckpoint.dataset_id == ds.id).first()
        is_s3 = connector.pipeline == "objects"
        us = resource.get("updated_since") if not is_s3 else None
        pk = [] if is_s3 else list(resource["primary_key"])
        wm_col = "source_modified" if is_s3 else (us["field"] if us else None)
        wm_kind = "timestamp" if is_s3 else (("integer" if us["format"] == "epoch" else "timestamp") if us else None)
        mode, fallback = "full_refresh", None
        if requested_mode == "incremental":
            if not is_s3 and not pk:
                fallback = "no_primary_key"
            elif not is_s3 and not us:
                fallback = "no_watermark"
            elif new_dataset:
                pass                                                       # the first run is the backfill
            elif checkpoint is None or (is_s3 and not (checkpoint.state or {}).get("objects")):
                fallback = "no_checkpoint"
            elif not storage.is_workspace_ref(ds.storage_ref):
                fallback = "previous_version_unavailable"
            else:
                mode = "incremental"
        prev_ref, prev_schema = ds.storage_ref, {}
        if mode == "full_refresh" and not new_dataset and storage.is_workspace_ref(prev_ref):
            try:
                prev_schema = ingest.read_schema(storage.ensure_local_workspace(prev_ref))
            except Exception:  # noqa: BLE001
                prev_schema = {}
        run.watermark_before = checkpoint.value if checkpoint else None
        run.mode, run.fallback_reason = mode, fallback
        db.commit()

        stats = {"read": 0, "inserted": 0, "updated": 0, "skipped": 0, "rejected": 0}
        reasons: dict = {}
        quarantine: list = []
        extra: dict = {"changes": [], "state": None, "max_wm": None, "deleted": 0, "unchanged": False, "table_stats": None}
        ctx = dict(db=db, run=run, connector=connector, cfg=cfg, secrets=secrets, resource=resource, curated=curated, stats=stats,
                   reasons=reasons, quarantine=quarantine, extra=extra, abort=abort, checkpoint=checkpoint, pk=pk, us=us,
                   wm_kind=wm_kind, prev_ref=prev_ref, table_name=table_name, connection=connection, ds=ds)
        deadline = time.monotonic() + config.CONNECTION_SYNC_MAX_SECONDS
        ctx["deadline"] = deadline
        if is_s3:
            try:
                _build_s3(ctx, tmp, mode == "incremental")
            except _Fallback as fb:                                       # a changed column set: rebuild everything
                for k in stats:
                    stats[k] = 0
                reasons.clear(), quarantine.clear()
                extra.update(changes=[], state=None, max_wm=None, deleted=0, unchanged=False)
                run.mode, run.fallback_reason = "full_refresh", fb.reason
                db.commit()
                if not new_dataset and storage.is_workspace_ref(prev_ref):
                    try:
                        prev_schema = ingest.read_schema(storage.ensure_local_workspace(prev_ref))
                    except Exception:  # noqa: BLE001
                        prev_schema = {}
                mode = "full_refresh"
                _build_s3(ctx, tmp, False)
        else:
            _build_rest(ctx, tmp, mode == "incremental")
        if extra["unchanged"]:                                              # nothing new in the source: no version, no swap
            run.status, run.finished_at, run.dataset_version = "skipped", ingest._now(), ds.version
            run.error_code, run.error_detail = "UNCHANGED_SOURCE", "Nothing changed in the source since the last run."
            run.records_read = run.records_skipped = stats["skipped"]
            db.commit()
            return ingest.ImportResult(ds, run, skipped=True)
        abort()
        changes = extra["changes"] + (ingest.diff_schema(prev_schema, ingest.read_schema(tmp)) if prev_schema else [])
        max_wm = extra["max_wm"]
        wm_after = None
        if wm_kind and wm_col:
            cur = parse_watermark(wm_kind, checkpoint.value) if (checkpoint and mode == "incremental") else None
            best = max_wm if cur is None or (max_wm is not None and max_wm > cur) else cur
            wm_after = format_watermark(wm_kind, best) if best is not None else (checkpoint.value if checkpoint else None)
        con = sqlite3.connect(tmp)
        try:
            total = con.execute(f"SELECT COUNT(*) FROM {_q(curated)}").fetchone()[0]
        finally:
            con.close()
        tstats = extra["table_stats"] or None
        publish(db, ds=ds, run=run, connection=connection, tmp=tmp, curated=curated, stats=stats, reasons=reasons, changes=changes,
                new_dataset=new_dataset, quota_check=quota_check, abort=abort, wm_col=wm_col, wm_kind=wm_kind, wm_after=wm_after,
                total=total, checkpoint=checkpoint, state=extra["state"], table_stats=tstats,
                sync_config={"mode": requested_mode, "watermark_column": wm_col, "watermark_type": wm_kind, "primary_key": pk,
                             "curated_table": curated, "source": {"kind": connector.name, "resource": table_name}})
    except BaseException as exc:
        db.rollback()
        ingest._fail(db, run, ds, exc, new_dataset)
        raise
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    if not new_dataset and prev_ref != ds.storage_ref:
        storage.delete_workspace_object(prev_ref)
    db.refresh(ds)
    db.refresh(run)
    quality.record(db, ds, run)
    return ingest.ImportResult(ds, run)


def _reject(ctx, reason: str, row_no: int, column: Optional[str] = None) -> None:
    ctx["stats"]["rejected"] += 1
    ctx["reasons"][reason] = ctx["reasons"].get(reason, 0) + 1
    if len(ctx["quarantine"]) < MAX_ROW_CODES:
        ctx["quarantine"].append((ctx["run"].id, ctx["curated"], row_no, column, reason, None))


def _finish_meta(ctx, con, source_name: str) -> None:
    _meta_tables(con)
    con.execute("DELETE FROM _qs_lineage WHERE table_name = ?", (ctx["curated"],))
    s = ctx["stats"]
    con.execute("INSERT INTO _qs_lineage VALUES (?,?,?,?,?,?,?)", (ctx["curated"], SOURCE_TYPES[ctx["connector"].name], ctx["connector"].name,
                                                                    source_name[:255], ctx["run"].id, ingest._now().isoformat(timespec="seconds"),
                                                                    s["inserted"] + s["updated"] + s["skipped"]))
    con.executemany("INSERT INTO _qs_quarantine VALUES (?,?,?,?,?,?)", ctx["quarantine"])


def _progress(ctx) -> None:
    s, run = ctx["stats"], ctx["run"]
    run.records_read, run.records_inserted, run.records_updated = s["read"], s["inserted"], s["updated"]
    run.records_skipped, run.records_rejected = s["skipped"], s["rejected"]
    ctx["db"].commit()


# ---------------------------------------------------------------------- REST

def _build_rest(ctx, tmp: str, incremental: bool) -> None:
    connector, cfg, secrets, res, curated, stats = ctx["connector"], ctx["cfg"], ctx["secrets"], ctx["resource"], ctx["curated"], ctx["stats"]
    pk, us, wm_kind, checkpoint, extra = ctx["pk"], ctx["us"], ctx["wm_kind"], ctx["checkpoint"], ctx["extra"]
    since = None
    if incremental:
        since = parse_watermark(wm_kind, checkpoint.value)
        if wm_kind == "timestamp":
            since = since - timedelta(seconds=config.INCREMENTAL_OVERLAP_SECONDS)
        shutil.copyfile(storage.ensure_local_workspace(ctx["prev_ref"]), tmp)
    con = sqlite3.connect(tmp)
    try:
        columns = _columns(con, curated) if incremental else []
        for records in connector.pages(cfg, secrets, res, since=since):
            ctx["abort"]()
            if time.monotonic() > ctx["deadline"]:
                raise SourceError("SYNC_TIMEOUT", "The sync took longer than the allowed time and was stopped.")
            flat = [flatten(r) for r in records]
            new_cols = []
            for r in flat:
                for k in r:
                    if k not in columns and k not in new_cols:
                        new_cols.append(k)
            if new_cols:
                if not columns:                                             # first page of a full refresh: create the table
                    missing = [k for k in pk if k not in new_cols]
                    if missing:
                        raise SourceError("INVALID_RESPONSE", "The configured primary_key field is not present in the records.")
                    cols_sql = ", ".join(f"{_q(k)} {sql_type(r.get(k) for r in flat)}" for k in new_cols)
                    pk_sql = f", PRIMARY KEY ({', '.join(_q(k) for k in pk)})" if pk else ""
                    con.execute(f"CREATE TABLE {_q(curated)} ({cols_sql}{pk_sql})")
                else:
                    for k in new_cols:
                        con.execute(f"ALTER TABLE {_q(curated)} ADD COLUMN {_q(k)} {sql_type(r.get(k) for r in flat)}")
                        extra["changes"].append({"table": curated, "change": "column_added", "column": k,
                                                 "to": sql_type(r.get(k) for r in flat)})
                columns = columns + new_cols
            if not columns:
                continue
            pk_idx = [columns.index(k) for k in pk]
            good = []
            for rec in flat:
                stats["read"] += 1
                if any(rec.get(k) is None for k in pk):
                    _reject(ctx, "missing_key", stats["read"], pk[0] if pk else None)
                    continue
                try:
                    cells = [to_cell(rec.get(c)) for c in columns]
                except UnsupportedValue as exc:
                    _reject(ctx, exc.reason, stats["read"])
                    continue
                if us and rec.get(us["field"]) is not None:
                    try:
                        v = parse_watermark(wm_kind, str(rec[us["field"]]))
                    except (ValueError, TypeError):
                        _reject(ctx, "invalid_watermark", stats["read"], us["field"])
                        continue
                    if extra["max_wm"] is None or v > extra["max_wm"]:
                        extra["max_wm"] = v
                good.append(cells)
            if stats["read"] > ingestion.MAX_INGEST_ROWS:
                raise ValueError(f"The source returned more than {ingestion.MAX_INGEST_ROWS} rows.")
            ins, upd, skip = _apply(con, curated, columns, pk_idx, good, upsert=bool(pk))
            stats["inserted"] += ins
            stats["updated"] += upd
            stats["skipped"] += skip
            _progress(ctx)
        if not columns:
            raise SourceError("EMPTY_SOURCE", "The resource returned no records, so there is nothing to import.")
        _finish_meta(ctx, con, f"{ctx['connection'].name}.{ctx['table_name']}")
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------- S3

def _build_s3(ctx, tmp: str, incremental: bool) -> None:
    connector, cfg, secrets, res, curated, stats, extra = (ctx["connector"], ctx["cfg"], ctx["secrets"], ctx["resource"], ctx["curated"],
                                                          ctx["stats"], ctx["extra"])
    from ingest import check_frame
    import pandas as pd
    objs = connector.list_objects(cfg, secrets, res)
    prev = dict(((ctx["checkpoint"].state or {}).get("objects") or {})) if incremental else {}
    sig = lambda o: [o["etag"], o["modified"], o["size"]]  # noqa: E731
    changed = [o for o in objs if prev.get(o["key"], [None])[:3] != sig(o)] if incremental else list(objs)
    current = {o["key"] for o in objs}
    gone = sorted(set(prev) - current)
    state = {k: v for k, v in prev.items() if k in current}
    for o in objs:
        if incremental and o not in changed:
            stats["skipped"] += int(prev[o["key"]][3])
            stats["read"] += int(prev[o["key"]][3])
    if incremental and not changed and not gone:
        extra["unchanged"] = True
        return
    if incremental:
        shutil.copyfile(storage.ensure_local_workspace(ctx["prev_ref"]), tmp)
    if os.path.exists(tmp) and not incremental:
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    total_bytes = 0
    deleted_rows = 0
    try:
        exists = incremental
        for key in gone:
            deleted_rows += con.execute(f"DELETE FROM {_q(curated)} WHERE source_object = ?", (key,)).rowcount
        for obj in changed:
            ctx["abort"]()
            if time.monotonic() > ctx["deadline"]:
                raise SourceError("SYNC_TIMEOUT", "The sync took longer than the allowed time and was stopped.")
            total_bytes += obj["size"]
            if total_bytes > config.S3_SOURCE_MAX_TOTAL_MB * 1024 * 1024:
                raise SourceError("TOO_MUCH_DATA", f"One run may read at most {config.S3_SOURCE_MAX_TOTAL_MB} MB; narrow the prefix.")
            df = connector.read_object(cfg, secrets, res, obj)
            keep, rejects, _dups = check_frame(df)
            for r in rejects:
                _reject(ctx, r["reason"], r["pos"] + 1, r["column"])
            good = df[keep.values] if len(df) else df
            good = good.copy()
            good["source_object"] = obj["key"]
            good["source_modified"] = obj["modified"]
            if exists:
                have = _columns(con, curated)
                if incremental and set(c for c in good.columns) != set(have):
                    raise _Fallback("schema_changed")
                for c in good.columns:
                    if c not in have:
                        decl = "INTEGER" if str(good[c].dtype).startswith(("int", "uint", "bool")) else "REAL" if str(good[c].dtype).startswith("float") else "TEXT"
                        con.execute(f"ALTER TABLE {_q(curated)} ADD COLUMN {_q(c)} {decl}")
                        extra["changes"].append({"table": curated, "change": "column_added", "column": str(c), "to": decl})
                if incremental:
                    con.execute(f"DELETE FROM {_q(curated)} WHERE source_object = ?", (obj["key"],))
            else:
                con.execute(pd.io.sql.get_schema(good, curated, con=con))
                exists = True
            good.to_sql(curated, con, if_exists="append", index=False)
            stats["read"] += len(df)
            if obj["key"] in prev:
                stats["updated"] += len(good)
            else:
                stats["inserted"] += len(good)
            state[obj["key"]] = sig(obj) + [len(good)]
            _progress(ctx)
        if not exists:
            raise SourceError("EMPTY_SOURCE", "No matching objects were found under the prefix, so there is nothing to import.")
        _finish_meta(ctx, con, f"s3://{cfg['bucket']}/{res['prefix']}")
        con.commit()
    finally:
        con.close()
    mods = [o["modified"] for o in objs]
    extra["max_wm"] = parse_watermark("timestamp", max(mods)) if mods else None
    extra["state"] = {"objects": state}
    extra["table_stats"] = {curated: {"read": stats["read"], "inserted": stats["inserted"], "updated": stats["updated"],
                                      "skipped": stats["skipped"], "rejected": stats["rejected"], "deleted": deleted_rows,
                                      "objects": len(objs), "objects_read": len(changed), "objects_removed": len(gone)}}
