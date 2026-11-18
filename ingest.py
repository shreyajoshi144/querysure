"""Layered ingestion (Sprint 3): Raw -> Validated -> Curated, with runs, quarantine, lineage and an atomic version swap.

  Raw       the original file, stored once per run (the last RAW_KEEP_RUNS runs are kept).
  Validated deterministic row checks run on the parsed rows before anything is written; rejected rows go to ``_qs_quarantine``.
  Curated   the typed tables the query engine sees, in one SQLite file per version. ``_qs_*`` tables are QuerySure's own
            (quarantine, lineage); the schema engine, the validator and the execution authorizer all refuse them.

A run builds the new curated file in a temp location, stores it under a NEW key, and only then points the dataset at it in one
database transaction (the swap). A run that fails, is interrupted or loses a race leaves the previous version untouched.
At most one run per dataset is active: a partial unique index, so it holds across processes.

Row rules (all deterministic, none changes a value): ``malformed_row`` (CSV line with more fields than the header),
``empty_row`` (every cell empty), ``missing_key`` (an ``id`` / ``*_id`` column that is otherwise unique and almost always set,
with a few blanks). Exact duplicate rows are legitimate data (two identical sales lines), so they are counted and reported
but kept. Nothing is coerced: a column whose type changes between runs is reported as a schema change.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import pandas as pd
from sqlalchemy.exc import IntegrityError

import quality
import storage
from connectors import registry
from connectors.base import DEFAULT_BATCH_ROWS, Rejects
from db import Dataset, IngestionRun
from modules import ingestion

logger = logging.getLogger("querysure.ingest")

RAW_KEEP_RUNS = 2
MAX_QUARANTINE_ROWS = 1000
ACTIVE = ("queued", "running")


class RunInProgress(ValueError):
    """Another run of this dataset is active (a ValueError so a job reports it instead of retrying)."""
    code = "RUN_IN_PROGRESS"


class IngestQuota(ValueError):
    code = "QUOTA_EXCEEDED"


@dataclass
class ImportResult:
    dataset: Dataset
    run: IngestionRun
    skipped: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fingerprint(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ schema

def read_schema(path: str) -> Dict[str, Dict[str, str]]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND substr(name, 1, 4) <> '_qs_'")]
        return {t: {c[1]: (c[2] or "") for c in con.execute(f'PRAGMA table_info("{t.replace(chr(34), chr(34) * 2)}")')} for t in tables}
    finally:
        con.close()


def _affinity(t: str) -> str:
    t = (t or "").upper()
    if "INT" in t:
        return "INTEGER"
    if any(k in t for k in ("REAL", "FLOA", "DOUB", "NUMERIC", "DEC")):
        return "REAL"
    return "TEXT"


def diff_schema(prev: Dict[str, Dict[str, str]], new: Dict[str, Dict[str, str]]) -> List[dict]:
    out: List[dict] = []
    for t in sorted(new.keys() - prev.keys()):
        out.append({"table": t, "change": "table_added"})
    for t in sorted(prev.keys() - new.keys()):
        out.append({"table": t, "change": "table_removed"})
    for t in sorted(prev.keys() & new.keys()):
        for c in sorted(new[t].keys() - prev[t].keys()):
            out.append({"table": t, "change": "column_added", "column": c, "to": _affinity(new[t][c])})
        for c in sorted(prev[t].keys() - new[t].keys()):
            out.append({"table": t, "change": "column_removed", "column": c, "from": _affinity(prev[t][c])})
        for c in sorted(prev[t].keys() & new[t].keys()):
            a, b = _affinity(prev[t][c]), _affinity(new[t][c])
            if a != b:  # reported, never coerced; INTEGER -> REAL only widens
                out.append({"table": t, "change": "type_changed", "column": c, "from": a, "to": b,
                            "compatible": (a, b) == ("INTEGER", "REAL")})
    return out


# ------------------------------------------------------------------ row validation

def check_frame(df: pd.DataFrame):
    """-> (keep_mask, rejects[{pos, column, reason}], duplicate_count). Positions are 0-based row positions in ``df``."""
    n = len(df)
    reason = pd.Series([None] * n, dtype=object, index=df.index)
    column: Dict[int, Optional[str]] = {}
    blank = df.isna().all(axis=1) if n else pd.Series([], dtype=bool)
    reason[blank] = "empty_row"
    for col in df.columns:
        lc = str(col).lower()
        if n < 20 or not (lc == "id" or lc.endswith("_id")):
            continue
        s = df[col]
        nn, nulls = int(s.notna().sum()), int(s.isna().sum())
        if 0 < nulls <= max(1, int(0.01 * n)) and nn and s.nunique(dropna=True) >= 0.99 * nn:
            hit = s.isna() & reason.isna()
            reason[hit] = "missing_key"
            for pos in [i for i, h in enumerate(hit.tolist()) if h]:
                column[pos] = str(col)
    rejects = [{"pos": i, "column": column.get(i), "reason": r} for i, r in enumerate(reason.tolist()) if r]
    try:
        dups = int(df.duplicated().sum())
    except TypeError:
        dups = 0
    return reason.isna(), rejects, dups


# ------------------------------------------------------------------ build

def _create_internal_tables(con: sqlite3.Connection) -> None:
    con.execute("CREATE TABLE _qs_quarantine (run_id TEXT, table_name TEXT, row_number INTEGER, column_name TEXT, "
                "reason TEXT, value TEXT)")
    con.execute("CREATE TABLE _qs_lineage (table_name TEXT, source_type TEXT, connector TEXT, source_name TEXT, "
                "run_id TEXT, ingested_at TEXT, row_count INTEGER)")


def build_curated(path: str, connector, dest: str, base_name: str, run_id: str, source_name: str,
                  batch_size: int = DEFAULT_BATCH_ROWS, progress: Optional[Callable[[dict], None]] = None) -> dict:
    """Write the curated database for one source into ``dest``. Returns counts; raises ValueError for an unreadable source."""
    stats: Dict[str, dict] = {}
    reasons: Counter = Counter()
    quarantine: List[tuple] = []
    duplicates = 0
    if connector.passthrough:
        ingestion.validate_sqlite(path)
        shutil.copyfile(path, dest)
        con = sqlite3.connect(dest)
        try:
            for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                n = con.execute(f'SELECT COUNT(*) FROM "{t.replace(chr(34), chr(34) * 2)}"').fetchone()[0]
                stats[t] = {"read": n, "inserted": n, "rejected": 0}
            _create_internal_tables(con)
            _write_lineage(con, stats, connector, source_name, run_id)
            con.commit()
        finally:
            con.close()
        return {"tables": stats, "reasons": {}, "duplicates": 0}
    bad = Rejects()
    frames = connector.read_tables(path, base_name, bad)
    con = sqlite3.connect(dest)
    try:
        for table, df in frames.items():
            keep, rejects, dups = check_frame(df)
            duplicates += dups
            malformed = [r for r in bad.rows if r.get("table") == table]
            for r in malformed:
                reasons[r["reason"]] += 1
                if len(quarantine) < MAX_QUARANTINE_ROWS:
                    quarantine.append((run_id, table, max(int(r["row"]) - 1, 1), None, r["reason"], None))
            for r in rejects:
                reasons[r["reason"]] += 1
                if len(quarantine) < MAX_QUARANTINE_ROWS:
                    quarantine.append((run_id, table, r["pos"] + 1, r["column"], r["reason"], None))
            clean = df[keep.values] if len(df) else df
            con.execute(pd.io.sql.get_schema(df, table, con=con))   # types come from the whole frame, as to_sql always did
            for start in range(0, len(clean), batch_size):
                clean.iloc[start:start + batch_size].to_sql(table, con, if_exists="append", index=False)
            stats[table] = {"read": len(df) + len(malformed), "inserted": len(clean),
                            "rejected": len(rejects) + len(malformed)}
            if progress:
                progress(stats)
            if stats[table]["read"] and not stats[table]["inserted"]:
                raise ValueError(f"Every row of '{table}' was rejected (check the file's delimiter and header row).")
        _create_internal_tables(con)
        con.executemany("INSERT INTO _qs_quarantine VALUES (?,?,?,?,?,?)", quarantine)
        _write_lineage(con, stats, connector, source_name, run_id)
        con.commit()
    finally:
        con.close()
    return {"tables": stats, "reasons": dict(reasons), "duplicates": duplicates}


def _write_lineage(con, stats, connector, source_name, run_id) -> None:
    con.executemany("INSERT INTO _qs_lineage VALUES (?,?,?,?,?,?,?)", [
        (t, "file", connector.name, (source_name or "")[:255], run_id, _now().isoformat(timespec="seconds"), s["inserted"])
        for t, s in stats.items()])


def quarantine_sample(curated_path: str, limit: int = 50) -> List[dict]:
    """First rows of the current version's quarantine. Reason codes and positions only: the row values are not stored."""
    con = sqlite3.connect(f"file:{curated_path}?mode=ro", uri=True)
    try:
        return [{"table": r[0], "row": r[1], "column": r[2], "reason": r[3]} for r in con.execute(
            "SELECT table_name, row_number, column_name, reason FROM _qs_quarantine ORDER BY rowid LIMIT ?", (limit,))]
    except sqlite3.DatabaseError:
        return []
    finally:
        con.close()


# ------------------------------------------------------------------ runs

def _reap_if_orphaned(db, active: Optional[IngestionRun]) -> None:
    if active is not None and active.job_id:
        import jobs
        jobs.fail_orphaned_jobs(db, job_id=active.job_id)


def active_run(db, dataset_id: str) -> Optional[IngestionRun]:
    return db.query(IngestionRun).filter(IngestionRun.dataset_id == dataset_id, IngestionRun.status.in_(ACTIVE)).first()


def start_run(db, ds: Dataset, *, user_id: str, job_id: Optional[str], connector: str, source_name: str,
              source_bytes: int, fp: str) -> IngestionRun:
    if job_id:  # a retried job continues its own run
        mine = db.query(IngestionRun).filter(IngestionRun.job_id == job_id, IngestionRun.status.in_(ACTIVE)).first()
        if mine:
            return mine
    for attempt in range(2):
        run = IngestionRun(dataset_id=ds.id, workspace_id=ds.workspace_id, user_id=user_id, job_id=job_id, connector=connector,
                           status="running", source_name=(source_name or "")[:255], source_bytes=source_bytes,
                           source_fingerprint=fp)
        db.add(run)
        try:
            db.commit()
            return run
        except IntegrityError:
            db.rollback()
            if attempt == 0:
                _reap_if_orphaned(db, active_run(db, ds.id))   # a run whose job died frees the slot
    raise RunInProgress("An import of this dataset is already running. Wait for it to finish, then try again.")


def mark_runs_interrupted(db, job_ids: List[str]) -> int:
    """Called when jobs are failed as orphans: their runs become ``interrupted`` and a never-finished new dataset is dropped."""
    if not job_ids:
        return 0
    runs = db.query(IngestionRun).filter(IngestionRun.job_id.in_(job_ids), IngestionRun.status.in_(ACTIVE)).all()
    for r in runs:
        r.status, r.error_code, r.finished_at = "interrupted", "JOB_INTERRUPTED", _now()
        r.error_detail = "The import was interrupted (the server restarted). The previous version, if any, is unchanged."
    db.commit()
    for r in runs:
        ds = db.get(Dataset, r.dataset_id)
        if ds is not None and ds.status == "importing" and (ds.version or 0) == 0:
            discard_dataset_row(db, ds)
    return len(runs)


def discard_dataset_row(db, ds: Dataset) -> None:
    from db import PipelineSession
    from db import DatasetQualityReport, DatasetSchedule, IngestionCheckpoint, Investigation
    db.query(DatasetQualityReport).filter(DatasetQualityReport.dataset_id == ds.id).delete(synchronize_session=False)
    import investigation
    investigation.delete_investigations(db, Investigation.dataset_id == ds.id)
    db.query(IngestionRun).filter(IngestionRun.dataset_id == ds.id).delete(synchronize_session=False)
    db.query(IngestionCheckpoint).filter(IngestionCheckpoint.dataset_id == ds.id).delete(synchronize_session=False)
    db.query(DatasetSchedule).filter(DatasetSchedule.dataset_id == ds.id).delete(synchronize_session=False)
    db.query(PipelineSession).filter(PipelineSession.dataset_id == ds.id).update(
        {PipelineSession.dataset_id: None}, synchronize_session=False)
    db.query(Dataset).filter(Dataset.id == ds.id).delete(synchronize_session=False)
    db.commit()


def _object_workspace(ds: Dataset) -> str:
    parsed = storage._parse_ws(ds.storage_ref)   # keep every object of a dataset under one prefix, even after a re-home
    return parsed[0] if parsed else ds.workspace_id


def _trim_raw(db, ds: Dataset) -> None:
    runs = (db.query(IngestionRun).filter(IngestionRun.dataset_id == ds.id, IngestionRun.status == "succeeded",
                                          IngestionRun.raw_ref.isnot(None)).order_by(IngestionRun.started_at.desc()).all())
    for old in runs[RAW_KEEP_RUNS:]:
        storage.delete_workspace_object(old.raw_ref)
        old.raw_ref = None
    db.commit()


# ------------------------------------------------------------------ orchestration

def import_source(db, *, user, workspace_id: str, dataset_id: Optional[str], raw_path: str, ext: str, fname: str,
                  job_id: Optional[str] = None, abort: Callable[[], None] = lambda: None,
                  quota_check: Optional[Callable[[int, Dataset], None]] = None) -> ImportResult:
    """Import ``raw_path`` as a new dataset (``dataset_id`` None) or as a new version of ``dataset_id``."""
    connector = registry.for_extension(ext)
    if connector is None:
        raise ValueError(f"Unsupported format: {ext}")
    fp, src_bytes = fingerprint(raw_path), os.path.getsize(raw_path)
    if dataset_id:
        ds = db.get(Dataset, dataset_id)
        if ds is None:
            raise ValueError("Dataset not found.")
        if ds.status == "ready" and (ds.version or 0) > 0 and ds.fingerprint == fp and ds.connector == connector.name:
            run = IngestionRun(dataset_id=ds.id, workspace_id=ds.workspace_id, user_id=user.id, job_id=job_id,
                               connector=connector.name, status="skipped", source_name=fname[:255], source_bytes=src_bytes,
                               source_fingerprint=fp, records_skipped=0, dataset_version=ds.version, finished_at=_now(),
                               error_code="UNCHANGED_SOURCE",
                               error_detail="The file is identical to the current version; nothing was imported.")
            db.add(run)
            db.commit()
            return ImportResult(ds, run, skipped=True)
    else:
        ds = Dataset(workspace_id=workspace_id, created_by=user.id, name=(fname or "Uploaded dataset")[:255], source_type="upload",
                     file_format=ext.lstrip(".").lower(), storage_ref=f"pending:{uuid.uuid4()}", status="importing", version=0,
                     connector=connector.name)
        db.add(ds)
        db.commit()
    new_dataset = (ds.version or 0) == 0
    try:
        run = start_run(db, ds, user_id=user.id, job_id=job_id, connector=connector.name, source_name=fname,
                        source_bytes=src_bytes, fp=fp)
    except BaseException:
        if new_dataset:
            discard_dataset_row(db, ds)
        raise
    tmp = os.path.join(storage.UPLOAD_DIR, f".build-{uuid.uuid4().hex}.db")
    raw_tmp = f"{raw_path}.{uuid.uuid4().hex[:8]}.raw"
    curated_ref = raw_ref = None
    try:
        os.makedirs(storage.UPLOAD_DIR, exist_ok=True)

        def progress(stats: dict) -> None:
            run.records_read = sum(s["read"] for s in stats.values())
            run.records_inserted = sum(s["inserted"] for s in stats.values())
            run.records_rejected = sum(s["rejected"] for s in stats.values())
            db.commit()

        built = build_curated(raw_path, connector, tmp, os.path.splitext(fname)[0], run.id, fname, progress=progress)
        abort()
        tstats = built["tables"]
        new_schema = read_schema(tmp)
        changes: List[dict] = []
        if not new_dataset and storage.is_workspace_ref(ds.storage_ref):
            try:
                changes = diff_schema(read_schema(storage.ensure_local_workspace(ds.storage_ref)), new_schema)
            except Exception:  # noqa: BLE001 - an unreadable previous version only costs the comparison
                changes = []
        new_bytes = os.path.getsize(tmp) + src_bytes
        if quota_check:
            quota_check(new_bytes, ds)
        obj_ws = _object_workspace(ds) if not new_dataset else ds.workspace_id
        shutil.copyfile(raw_path, raw_tmp)
        raw_ref = storage.store_workspace_object(raw_tmp, obj_ws, ds.id, f"raw/{run.id}{ext.lower()}")
        curated_size = os.path.getsize(tmp)
        curated_ref = storage.store_workspace_object(tmp, obj_ws, ds.id, f"curated-{run.id}.db", cache=True)
        abort()
        prev_ref = ds.storage_ref
        ds.storage_ref, ds.status, ds.connector, ds.fingerprint = curated_ref, "ready", connector.name, fp
        ds.bytes = curated_size + src_bytes
        ds.table_count = len(tstats)
        ds.row_count = sum(s["inserted"] for s in tstats.values())
        ds.version = (ds.version or 0) + 1
        if changes and not new_dataset:
            ds.schema_version = (ds.schema_version or 1) + 1
        run.status, run.finished_at, run.dataset_version = "succeeded", _now(), ds.version
        run.records_read = sum(s["read"] for s in tstats.values())
        run.records_inserted = ds.row_count
        run.records_rejected = sum(s["rejected"] for s in tstats.values())
        run.duplicate_rows, run.reject_reasons, run.table_stats = built["duplicates"], built["reasons"], tstats
        run.schema_changes, run.curated_ref, run.raw_ref = changes, curated_ref, raw_ref
        db.commit()   # the swap: readers see the old version or the new one, never a mixture
    except BaseException as exc:
        db.rollback()
        for ref in (curated_ref, raw_ref):
            storage.delete_workspace_object(ref)
        _fail(db, run, ds, exc, new_dataset)
        raise
    finally:
        for p in (tmp, raw_tmp):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
    if not new_dataset and prev_ref != curated_ref:
        storage.delete_workspace_object(prev_ref)
    _trim_raw(db, ds)
    db.refresh(ds)
    db.refresh(run)
    quality.record(db, ds, run)
    return ImportResult(ds, run)


def _fail(db, run: IngestionRun, ds: Dataset, exc: BaseException, new_dataset: bool) -> None:
    try:
        from observability.redaction import scrub_text
        db.refresh(run)
        if run.status in ACTIVE:
            run.status = "failed"
            run.error_code = getattr(exc, "code", None) or ("INVALID_SOURCE" if isinstance(exc, ValueError) else "IMPORT_FAILED")
            run.error_detail = scrub_text(str(exc), 300) if isinstance(exc, ValueError) else "The import failed unexpectedly."
            run.finished_at = _now()
            db.commit()
        if new_dataset and (ds.version or 0) == 0:
            discard_dataset_row(db, ds)
    except Exception as e:  # noqa: BLE001 - never mask the original failure
        db.rollback()
        logger.warning("could not record a failed run: %s", type(e).__name__)
