"""Connectors, dataset imports, ingestion runs and "open a catalog dataset" routes (Sprint 3).

Every route authenticates with ``get_current_user`` and authorizes through ``workspaces.require_role`` (viewer+ read and open,
analyst+ create, re-import and read the quarantine sample). A non-member sees 404 for every dataset. Responses never contain
storage references; row values are not stored anywhere, so a quarantine sample is reason codes and positions only.
"""
from __future__ import annotations

import os
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as OrmSession

import ingest
import quotas
import storage
import workspaces as ws_service
from auth import get_current_user
from connectors import registry
from db import Dataset, DatasetQualityReport, IngestionRun, SessionConflictError, SessionNotFoundError, User, get_db, get_or_create_session, update_session
from modules.ingestion import sanitize_filename

RUNS_PAGE_MAX = 50


class OpenIn(BaseModel):
    session_id: str = Field(..., max_length=64)


def _ok(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content={"success": True, **data})


def _err(message: str, code: int, error_code: str) -> JSONResponse:
    return JSONResponse(status_code=code, content={"success": False, "error": message, "error_code": error_code})


def _run_dict(r: IngestionRun, role: str) -> dict:
    can_detail = ws_service.ROLE_RANK.get(role, 0) >= ws_service.ROLE_RANK["analyst"]
    return {
        "id": r.id, "status": r.status, "connector": r.connector, "mode": r.mode, "source_name": r.source_name,
        "source_bytes": r.source_bytes, "records_read": r.records_read, "records_inserted": r.records_inserted,
        "records_updated": r.records_updated, "records_skipped": r.records_skipped, "records_rejected": r.records_rejected,
        "duplicate_rows": r.duplicate_rows, "reject_reasons": r.reject_reasons or {}, "table_stats": r.table_stats or {},
        "schema_changes": r.schema_changes or [], "dataset_version": r.dataset_version, "error_code": r.error_code,
        "error_detail": r.error_detail if can_detail else None,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }


def build_router(limiter, *, upload_limits: dict, stream_to_disk, multipart_overhead: int, upload_dir) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["datasets"])

    def _dataset(db: OrmSession, user: User, dataset_id: str, min_role: str):
        ds = db.get(Dataset, dataset_id) if 0 < len(dataset_id or "") <= 64 else None
        if ds is None:
            raise ws_service.WorkspaceError("DATASET_NOT_FOUND", "Dataset not found.", 404)
        try:
            _, member = ws_service.require_role(db, user, ds.workspace_id, min_role)
        except ws_service.WorkspaceError as exc:
            if exc.status_code == 404:   # not a member: do not reveal that the dataset exists
                raise ws_service.WorkspaceError("DATASET_NOT_FOUND", "Dataset not found.", 404)
            raise
        return ds, member

    def _wserr(exc: ws_service.WorkspaceError) -> JSONResponse:
        return _err(exc.message, exc.status_code, exc.code)

    def _stage(request: Request, file: UploadFile, user: User):
        """Validate and stream an upload to disk, then stage it durably. Returns (fname, ext, size, object_ref) or an error response."""
        fname = sanitize_filename(file.filename)
        ext = os.path.splitext(fname)[1].lower()
        if ext not in upload_limits or registry.for_extension(ext) is None:
            return _err("Unsupported file type. Upload a .csv, .json, .xlsx, .xlsm, .parquet, .db, .sqlite, or .sqlite3 file.",
                        400, "UNSUPPORTED_FILE_TYPE")
        limit = upload_limits[ext]
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit + multipart_overhead:
            return _err(f"File too large. Maximum allowed size is {limit // (1024 * 1024)} MB.", 413, "FILE_TOO_LARGE")
        raw_path = os.path.join(upload_dir(), f"{uuid.uuid4()}{ext}")
        if not stream_to_disk(file, raw_path, limit):
            return _err(f"File too large. Maximum allowed size is {limit // (1024 * 1024)} MB.", 413, "FILE_TOO_LARGE")
        size = os.path.getsize(raw_path)
        try:
            object_ref = storage.stage_upload(raw_path, user.id, ext)
        except storage.StorageError:
            storage.discard_local(raw_path)
            return _err("Uploaded file could not be stored. Please retry shortly.", 503, "STORAGE_UNAVAILABLE")
        if object_ref != raw_path:
            storage.discard_local(raw_path)
        return fname, ext, size, object_ref

    def _enqueue(db, user, payload, object_ref, background_tasks, reserve: Optional[int]):
        from jobs import enqueue_job
        try:
            if reserve is not None:   # new datasets reserve quota exactly as session uploads do
                quotas.lock_and_reserve_upload(db, user, reserve, None)
            job = enqueue_job(db, user.id, "dataset_ingestion", payload, background_tasks)
        except quotas.QuotaExceeded as exc:
            storage.delete_staged(object_ref, user.id) if object_ref.startswith("s3://") else storage.discard_local(object_ref)
            return JSONResponse(status_code=403, content=exc.as_payload())
        except Exception:
            storage.delete_staged(object_ref, user.id) if object_ref.startswith("s3://") else storage.discard_local(object_ref)
            raise
        return job

    @router.get("/connectors")
    def list_connectors(current_user: User = Depends(get_current_user)):
        return _ok({"connectors": registry.describe()})

    @router.post("/datasets")
    @limiter.limit("10/minute")
    def create_dataset(request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...), workspace_id: str = Form(None),
                       current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws_id = ws_service.upload_workspace_id(db, current_user, workspace_id)
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        staged = _stage(request, file, current_user)
        if isinstance(staged, JSONResponse):
            return staged
        fname, ext, size, object_ref = staged
        try:
            quotas.check_upload_quota(db, current_user, size, None)
        except quotas.QuotaExceeded as exc:
            storage.delete_staged(object_ref, current_user.id) if object_ref.startswith("s3://") else storage.discard_local(object_ref)
            return JSONResponse(status_code=403, content=exc.as_payload())
        job = _enqueue(db, current_user, {"object_ref": object_ref, "ext": ext, "fname": fname, "size": size, "workspace_id": ws_id},
                       object_ref, background_tasks, reserve=size)
        if isinstance(job, JSONResponse):
            return job
        return _ok({"job_id": job.id}, 202)

    @router.post("/datasets/{dataset_id}/runs")
    @limiter.limit("10/minute")
    def reimport(request: Request, dataset_id: str, background_tasks: BackgroundTasks, file: UploadFile = File(...),
                 current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ds, _ = _dataset(db, current_user, dataset_id, "analyst")
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        if ds.connection_id or ds.source_type in ("database", "api", "object_storage"):
            return _err("This dataset syncs from a database connection; use Sync instead of uploading a file.", 409, "SOURCE_IS_CONNECTION")
        if ds.status != "ready":
            return _err("This dataset is still being imported.", 409, "RUN_IN_PROGRESS")
        if ingest.active_run(db, ds.id):
            return _err("An import of this dataset is already running. Wait for it to finish, then try again.", 409, "RUN_IN_PROGRESS")
        staged = _stage(request, file, current_user)
        if isinstance(staged, JSONResponse):
            return staged
        fname, ext, size, object_ref = staged
        job = _enqueue(db, current_user, {"object_ref": object_ref, "ext": ext, "fname": fname, "source_size": size,
                                          "dataset_id": ds.id, "workspace_id": ds.workspace_id}, object_ref, background_tasks, reserve=None)
        if isinstance(job, JSONResponse):
            return job
        return _ok({"job_id": job.id, "dataset_id": ds.id}, 202)

    @router.get("/datasets/{dataset_id}/runs")
    def list_runs(dataset_id: str, limit: int = 20, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ds, member = _dataset(db, current_user, dataset_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        limit = max(1, min(int(limit or 20), RUNS_PAGE_MAX))
        rows = (db.query(IngestionRun).filter(IngestionRun.dataset_id == ds.id)
                .order_by(IngestionRun.started_at.desc()).limit(limit).all())
        return _ok({"runs": [_run_dict(r, member.role) for r in rows], "dataset": {"id": ds.id, "name": ds.name, "version": ds.version,
                                                                                    "status": ds.status}, "your_role": member.role})

    @router.get("/datasets/{dataset_id}/runs/{run_id}")
    def get_run(dataset_id: str, run_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ds, member = _dataset(db, current_user, dataset_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        run = db.get(IngestionRun, run_id) if 0 < len(run_id) <= 64 else None
        if run is None or run.dataset_id != ds.id:
            return _err("Run not found.", 404, "RUN_NOT_FOUND")
        return _ok({"run": _run_dict(run, member.role)})

    @router.get("/datasets/{dataset_id}/quality")
    def quality_reports(dataset_id: str, limit: int = 10, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        """Data-quality report of the latest version (full profile) plus a short history of earlier ones (viewer+)."""
        try:
            ds, _ = _dataset(db, current_user, dataset_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        limit = max(1, min(int(limit or 10), 50))
        rows = (db.query(DatasetQualityReport).filter(DatasetQualityReport.dataset_id == ds.id)
                .order_by(DatasetQualityReport.created_at.desc()).limit(limit).all())
        def brief(r):
            return {"run_id": r.run_id, "dataset_version": r.dataset_version, "status": r.status, "findings": len(r.findings or []),
                    "created_at": r.created_at.isoformat() if r.created_at else None}
        latest = rows[0] if rows else None
        return _ok({"latest": ({**brief(latest), "profile": latest.profile, "findings_list": latest.findings} if latest else None),
                    "history": [brief(r) for r in rows], "dataset": {"id": ds.id, "version": ds.version}})

    @router.get("/datasets/{dataset_id}/quarantine")
    def quarantine(dataset_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        """Reason codes and row positions of rows rejected by the CURRENT version's import (analyst+)."""
        try:
            ds, _ = _dataset(db, current_user, dataset_id, "analyst")
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        if ds.status != "ready" or not storage.is_workspace_ref(ds.storage_ref):
            return _ok({"rows": [], "version": ds.version})
        try:
            path = storage.ensure_local_workspace(ds.storage_ref)
        except storage.DatasetUnavailable:
            return _err("The stored dataset is no longer available.", 410, "DATASET_UNAVAILABLE")
        except storage.StorageError:
            return _err("Dataset storage is temporarily unavailable.", 503, "STORAGE_UNAVAILABLE")
        return _ok({"rows": ingest.quarantine_sample(path, 50), "version": ds.version})

    @router.post("/datasets/{dataset_id}/open")
    @limiter.limit("30/minute")
    def open_dataset(request: Request, dataset_id: str, payload: OpenIn, current_user: User = Depends(get_current_user),
                     db: OrmSession = Depends(get_db)):
        """Make a catalog dataset the dataset of one of the caller's sessions (viewer+)."""
        import jobs
        from modules import build_semantic_layer, build_vector_store, clear_cache_key, load_database
        try:
            ds, _ = _dataset(db, current_user, dataset_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        if ds.status != "ready" or not storage.is_workspace_ref(ds.storage_ref):
            return _err("This dataset is not ready to open.", 409, "DATASET_IMPORTING")
        try:
            row = get_or_create_session(db, user_id=current_user.id, session_id=payload.session_id)
        except SessionNotFoundError:
            return _err("Session not found.", 404, "SESSION_NOT_FOUND")
        try:
            path = storage.ensure_local_workspace(ds.storage_ref)
        except storage.DatasetUnavailable:
            return _err("The stored dataset is no longer available.", 410, "DATASET_UNAVAILABLE")
        except storage.StorageError:
            return _err("Dataset storage is temporarily unavailable.", 503, "STORAGE_UNAVAILABLE")
        schema = load_database(path)
        semantic = build_semantic_layer(schema)
        old_ref = row.db_path
        clear_cache_key(row.id)
        build_vector_store(semantic, cache_key=row.id)
        try:
            row = update_session(db, row, db_path=ds.storage_ref, dataset_bytes=None, dataset_id=ds.id, dataset_version=ds.version,
                                 schema=schema, semantic=semantic, retrieval=None, sql=None, sql_result=None, question=None,
                                 validation=None, query_result=None, reliability=None, insights=None)
        except SessionConflictError:
            return _err("The session was changed by another request. Try again.", 409, "SESSION_CONFLICT")
        jobs._cleanup_superseded(db, old_ref, ds.storage_ref, row.id)   # legacy uploads only; workspace datasets are never auto-deleted
        return _ok({"session_id": row.id, "db_name": ds.name, "dataset_id": ds.id, "workspace_id": ds.workspace_id,
                    "table_count": schema["table_count"], "total_rows": schema["total_rows"],
                    "relationships": len(schema["relationships"]), "db_size": schema["db_size"],
                    "tables": jobs._schema_summary(schema), "health_score": jobs._calc_health(schema)})

    return router
