"""Database connections and connection-backed datasets (Sprint 4).

Roles: admin+ creates, edits, enables / disables and deletes a connection (it holds a credential); analyst+ lists, tests and
browses connections and imports or syncs tables from them; viewers see only a dataset's source summary. Passwords are encrypted
at rest (secrets_store.py) and are never returned: a connection is shown with ``has_password`` only. Changing the host, port or
user requires entering the password again, so a stored credential can never be redirected to another server.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

import audit
import ingest
import secrets_store
import workspaces as ws_service
from auth import get_current_user
from connectors import registry
from connectors.database import ConnectorError, table_by_name
import config
from db import Connection, Dataset, DatasetSchedule, IngestionCheckpoint, IngestionRun, User, get_db


class ConnectionIn(BaseModel):
    workspace_id: Optional[str] = Field(default=None, max_length=64)
    name: str = Field(..., max_length=500)
    connector: str = Field(..., max_length=32)
    config: dict
    password: Optional[str] = Field(default=None, max_length=1024)


class ConnectionPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=500)
    config: Optional[dict] = None
    password: Optional[str] = Field(default=None, max_length=1024)


class TableImportIn(BaseModel):
    table: str = Field(..., max_length=255)
    mode: str = Field(default="full_refresh", max_length=16)
    watermark_column: Optional[str] = Field(default=None, max_length=255)
    name: Optional[str] = Field(default=None, max_length=255)


class SyncIn(BaseModel):
    mode: str = Field(default="incremental", max_length=16)


class ScheduleIn(BaseModel):
    interval_minutes: int
    mode: str = Field(default="incremental", max_length=16)
    enabled: bool = True


def _ok(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content={"success": True, **data})


def _err(message: str, code: int, error_code: str) -> JSONResponse:
    return JSONResponse(status_code=code, content={"success": False, "error": message, "error_code": error_code})


def _iso(v):
    return v.isoformat() if v else None


def connection_to_dict(c: Connection, role: str) -> dict:
    cfg = c.config or {}
    return {
        "id": c.id, "workspace_id": c.workspace_id, "name": c.name, "connector": c.connector, "status": c.status,
        "summary": registry.source(c.connector).summarize(cfg) if registry.source(c.connector) else "",
        "host": cfg.get("host"), "port": cfg.get("port"), "database": cfg.get("database"), "user": cfg.get("user"),
        "ssl_mode": cfg.get("ssl_mode"), "schema": cfg.get("schema"), "has_password": bool(c.secret_ciphertext),
        "last_test_at": _iso(c.last_test_at), "last_test_ok": c.last_test_ok, "last_test_error": c.last_test_error,
        "can_manage": ws_service.ROLE_RANK.get(role, 0) >= ws_service.ROLE_RANK["admin"],
        "created_at": _iso(c.created_at),
    }


def build_router(limiter) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["connections"])

    def _conn(db: OrmSession, user: User, connection_id: str, min_role: str):
        c = db.get(Connection, connection_id) if 0 < len(connection_id or "") <= 64 else None
        if c is None:
            raise ws_service.WorkspaceError("CONNECTION_NOT_FOUND", "Connection not found.", 404)
        try:
            _, member = ws_service.require_role(db, user, c.workspace_id, min_role)
        except ws_service.WorkspaceError as exc:
            if exc.status_code == 404:
                raise ws_service.WorkspaceError("CONNECTION_NOT_FOUND", "Connection not found.", 404)
            raise
        return c, member

    def _werr(exc: ws_service.WorkspaceError) -> JSONResponse:
        return _err(exc.message, exc.status_code, exc.code)

    def _secrets(c: Connection) -> dict:
        return secrets_store.decrypt(c.id, c.secret_ciphertext) if c.secret_ciphertext else {}

    def _open_schema(c: Connection):
        """Tables of a connection, or an error response. Errors are codes, never driver text."""
        connector = registry.source(c.connector)
        try:
            return connector.discover_schema(c.config, _secrets(c)), None
        except ConnectorError as exc:
            return None, _err(exc.message, 502 if exc.transient else 422, exc.code)
        except secrets_store.SecretsUnavailable:
            return None, _err("The saved password cannot be used; edit the connection and enter it again.", 409, "SECRETS_UNAVAILABLE")

    @router.get("/connections")
    def list_connections(workspace_id: Optional[str] = None, current_user: User = Depends(get_current_user),
                         db: OrmSession = Depends(get_db)):
        try:
            ws_id = ws_service.upload_workspace_id(db, current_user, None) if not workspace_id else workspace_id
            ws, member = ws_service.require_role(db, current_user, ws_id, "analyst")
        except ws_service.WorkspaceError as exc:
            return _werr(exc)
        rows = db.query(Connection).filter(Connection.workspace_id == ws.id).order_by(Connection.created_at).all()
        return _ok({"connections": [connection_to_dict(c, member.role) for c in rows], "your_role": member.role,
                    "encryption_configured": secrets_store.configured()})

    @router.post("/connections")
    @limiter.limit("10/minute")
    def create_connection(request: Request, payload: ConnectionIn, current_user: User = Depends(get_current_user),
                          db: OrmSession = Depends(get_db)):
        try:
            ws_id = payload.workspace_id or ws_service.personal_workspace_id(current_user.id)
            ws, _ = ws_service.require_role(db, current_user, ws_id, "admin")
        except ws_service.WorkspaceError as exc:
            return _werr(exc)
        connector = registry.source(payload.connector)
        if connector is None:
            return _err("Unsupported connection type.", 400, "UNSUPPORTED_CONNECTOR")
        if not secrets_store.configured():
            return _err("Saved connections are not configured on this server (CONNECTIONS_ENCRYPTION_KEY is not set).", 503,
                        "CONNECTIONS_NOT_CONFIGURED")
        name = " ".join((payload.name or "").split())
        if not name or len(name) > 120:
            return _err("Connection name is required (at most 120 characters).", 422, "INVALID_NAME")
        try:
            cfg = connector.validate_config(payload.config)
        except ValueError as exc:
            return _err(str(exc), 422, "INVALID_CONFIG")
        row = Connection(workspace_id=ws.id, created_by=current_user.id, name=name, connector=connector.name, config=cfg)
        db.add(row)
        try:
            db.flush()
            row.secret_ciphertext = secrets_store.encrypt(row.id, {"password": payload.password}) if payload.password else None
            db.commit()
        except IntegrityError:
            db.rollback()
            return _err("A connection with that name already exists in this workspace.", 409, "NAME_TAKEN")
        audit.record(ws.id, "connection.create", actor_id=current_user.id, target_type="connection", target_id=row.id,
                     details={"name": name, "connector": connector.name})
        return _ok({"connection": connection_to_dict(row, "admin")}, 201)

    @router.get("/connections/{connection_id}")
    def get_connection(connection_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            c, member = _conn(db, current_user, connection_id, "analyst")
        except ws_service.WorkspaceError as exc:
            return _werr(exc)
        return _ok({"connection": connection_to_dict(c, member.role)})

    @router.patch("/connections/{connection_id}")
    @limiter.limit("20/minute")
    def update_connection(request: Request, connection_id: str, payload: ConnectionPatch = Body(...),
                          current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            c, _ = _conn(db, current_user, connection_id, "admin")
        except ws_service.WorkspaceError as exc:
            return _werr(exc)
        connector = registry.source(c.connector)
        if payload.config is not None:
            try:
                new_cfg = connector.validate_config({**(c.config or {}), **payload.config})
            except ValueError as exc:
                return _err(str(exc), 422, "INVALID_CONFIG")
            moved = any(new_cfg.get(k) != (c.config or {}).get(k) for k in connector.credential_keys)
            if moved and not payload.password:
                # A stored secret must never follow a changed server, endpoint, account or auth method to a different place.
                return _err("Changing where this connection points or who it signs in as requires entering the password again.", 422,
                            "PASSWORD_REQUIRED")
            c.config = new_cfg
        if payload.password:
            if not secrets_store.configured():
                return _err("Saved connections are not configured on this server.", 503, "CONNECTIONS_NOT_CONFIGURED")
            c.secret_ciphertext = secrets_store.encrypt(c.id, {"password": payload.password})
        if payload.name is not None:
            name = " ".join(payload.name.split())
            if not name or len(name) > 120:
                return _err("Connection name is required (at most 120 characters).", 422, "INVALID_NAME")
            c.name = name
        c.last_test_at = c.last_test_ok = c.last_test_error = None
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return _err("A connection with that name already exists in this workspace.", 409, "NAME_TAKEN")
        audit.record(c.workspace_id, "connection.update", actor_id=current_user.id, target_type="connection", target_id=c.id,
                     details={"name": c.name, "connector": c.connector})
        return _ok({"connection": connection_to_dict(c, "admin")})

    def _set_status(connection_id: str, status: str, user: User, db: OrmSession):
        try:
            c, _ = _conn(db, user, connection_id, "admin")
        except ws_service.WorkspaceError as exc:
            return _werr(exc)
        c.status = status
        db.commit()
        audit.record(c.workspace_id, "connection." + ("disable" if status == "disabled" else "enable"), actor_id=user.id,
                     target_type="connection", target_id=c.id, details={"name": c.name, "connector": c.connector})
        return _ok({"connection": connection_to_dict(c, "admin")})

    @router.post("/connections/{connection_id}/disable")
    @limiter.limit("20/minute")
    def disable(request: Request, connection_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        return _set_status(connection_id, "disabled", current_user, db)

    @router.post("/connections/{connection_id}/enable")
    @limiter.limit("20/minute")
    def enable(request: Request, connection_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        return _set_status(connection_id, "active", current_user, db)

    @router.delete("/connections/{connection_id}")
    @limiter.limit("20/minute")
    def delete_connection(request: Request, connection_id: str, current_user: User = Depends(get_current_user),
                          db: OrmSession = Depends(get_db)):
        """Delete a connection and its stored secret. Datasets imported from it keep their data (they just stop syncing)."""
        try:
            c, _ = _conn(db, current_user, connection_id, "admin")
        except ws_service.WorkspaceError as exc:
            return _werr(exc)
        active = (db.query(IngestionRun.id).join(Dataset, Dataset.id == IngestionRun.dataset_id)
                  .filter(Dataset.connection_id == c.id, IngestionRun.status.in_(ingest.ACTIVE)).first())
        if active:
            return _err("A sync from this connection is running. Wait for it to finish.", 409, "RUN_IN_PROGRESS")
        detached = db.query(Dataset).filter(Dataset.connection_id == c.id).update({Dataset.connection_id: None}, synchronize_session=False)
        ws_id, name, connector = c.workspace_id, c.name, c.connector
        db.delete(c)
        db.commit()
        audit.record(ws_id, "connection.delete", actor_id=current_user.id, target_type="connection", target_id=connection_id,
                     details={"name": name, "connector": connector, "count": detached})
        return _ok({"deleted": True, "datasets_detached": detached})

    @router.post("/connections/{connection_id}/test")
    @limiter.limit("10/minute")
    def test_connection(request: Request, connection_id: str, current_user: User = Depends(get_current_user),
                        db: OrmSession = Depends(get_db)):
        try:
            c, _ = _conn(db, current_user, connection_id, "analyst")
        except ws_service.WorkspaceError as exc:
            return _werr(exc)
        if c.status != "active":
            return _err("This connection is disabled.", 409, "CONNECTION_DISABLED")
        connector = registry.source(c.connector)
        ok, code, message = True, None, ""
        try:
            message = connector.test_connection(c.config, _secrets(c)).message
        except ConnectorError as exc:
            ok, code, message = False, exc.code, exc.message
        except secrets_store.SecretsUnavailable:
            ok, code, message = False, "SECRETS_UNAVAILABLE", "The saved password cannot be used; enter it again."
        c.last_test_at, c.last_test_ok, c.last_test_error = datetime.now(timezone.utc), ok, code
        db.commit()
        return _ok({"ok": ok, "error_code": code, "message": message})

    @router.get("/connections/{connection_id}/schema")
    def connection_schema(connection_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            c, _ = _conn(db, current_user, connection_id, "analyst")
        except ws_service.WorkspaceError as exc:
            return _werr(exc)
        if c.status != "active":
            return _err("This connection is disabled.", 409, "CONNECTION_DISABLED")
        tables, error = _open_schema(c)
        if error is not None:
            return error
        return _ok({"tables": [{
            "name": t.name, "is_view": t.is_view, "primary_key": t.primary_key,
            "incremental_ready": t.incremental_ready if t.incremental_ready is not None
            else bool(t.primary_key) and any(col.kind != "other" for col in t.columns),
            "watermark": t.watermark,
            "columns": [{"name": col.name, "type": col.type, "watermark_ok": col.kind != "other"} for col in t.columns]} for t in tables]})

    def _enqueue(db, user, payload, background_tasks):
        from jobs import enqueue_job
        return enqueue_job(db, user.id, "dataset_ingestion", payload, background_tasks)

    @router.post("/connections/{connection_id}/datasets")
    @limiter.limit("10/minute")
    def import_table(request: Request, connection_id: str, payload: TableImportIn, background_tasks: BackgroundTasks,
                     current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        """Import one table as a new dataset. ``mode`` incremental needs a primary key and an integer / timestamp watermark column;
        its first run is the full backfill."""
        try:
            c, _ = _conn(db, current_user, connection_id, "analyst")
        except ws_service.WorkspaceError as exc:
            return _werr(exc)
        if c.status != "active":
            return _err("This connection is disabled.", 409, "CONNECTION_DISABLED")
        if payload.mode not in ("full_refresh", "incremental"):
            return _err("mode must be full_refresh or incremental.", 422, "INVALID_MODE")
        tables, error = _open_schema(c)
        if error is not None:
            return error
        table = table_by_name(tables, payload.table)
        if table is None:
            return _err("That table was not found (or is not visible to this connection's user).", 404, "TABLE_NOT_FOUND")
        if payload.mode == "incremental" and table.incremental_ready is not None:      # REST / S3 decide for themselves
            if not table.incremental_ready:
                return _err("Incremental sync is not available for this resource (a REST resource needs primary_key and updated_since).",
                            422, "INCREMENTAL_NOT_AVAILABLE")
        elif payload.mode == "incremental":
            col = table.column(payload.watermark_column or "")
            if not table.primary_key:
                return _err("Incremental sync needs a primary key; this table has none. Use full refresh.", 422, "NO_PRIMARY_KEY")
            if col is None or col.kind == "other":
                return _err("Choose an integer or timestamp column as the watermark.", 422, "INVALID_WATERMARK")
        job = _enqueue(db, current_user, {"connection_id": c.id, "workspace_id": c.workspace_id, "table": table.name, "mode": payload.mode,
                                          "watermark_column": payload.watermark_column, "name": payload.name}, background_tasks)
        return _ok({"job_id": job.id}, 202)

    @router.post("/datasets/{dataset_id}/sync")
    @limiter.limit("20/minute")
    def sync_dataset(request: Request, dataset_id: str, background_tasks: BackgroundTasks, payload: SyncIn = Body(default_factory=SyncIn),
                     current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        ds = db.get(Dataset, dataset_id) if 0 < len(dataset_id or "") <= 64 else None
        try:
            if ds is None:
                raise ws_service.WorkspaceError("DATASET_NOT_FOUND", "Dataset not found.", 404)
            ws_service.require_role(db, current_user, ds.workspace_id, "analyst")
        except ws_service.WorkspaceError as exc:
            return _werr(ws_service.WorkspaceError("DATASET_NOT_FOUND", "Dataset not found.", 404) if exc.status_code == 404 else exc)
        if payload.mode not in ("full_refresh", "incremental"):
            return _err("mode must be full_refresh or incremental.", 422, "INVALID_MODE")
        if not ds.connection_id or not ds.source_table:
            return _err("This dataset does not sync from a connection.", 409, "NOT_A_CONNECTION_DATASET")
        c = db.get(Connection, ds.connection_id)
        if c is None or c.status != "active":
            return _err("The connection is disabled or deleted.", 409, "CONNECTION_DISABLED")
        if ds.status != "ready" or ingest.active_run(db, ds.id):
            return _err("An import of this dataset is already running. Wait for it to finish, then try again.", 409, "RUN_IN_PROGRESS")
        job = _enqueue(db, current_user, {"connection_id": c.id, "workspace_id": ds.workspace_id, "table": ds.source_table,
                                          "mode": payload.mode, "dataset_id": ds.id}, background_tasks)
        return _ok({"job_id": job.id, "dataset_id": ds.id}, 202)

    @router.get("/datasets/{dataset_id}/source")
    def dataset_source(dataset_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        """Where a dataset comes from and how far it has synced (viewer+). No host, user or secret is included."""
        ds = db.get(Dataset, dataset_id) if 0 < len(dataset_id or "") <= 64 else None
        try:
            if ds is None:
                raise ws_service.WorkspaceError("DATASET_NOT_FOUND", "Dataset not found.", 404)
            ws_service.require_role(db, current_user, ds.workspace_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _werr(ws_service.WorkspaceError("DATASET_NOT_FOUND", "Dataset not found.", 404) if exc.status_code == 404 else exc)
        c = db.get(Connection, ds.connection_id) if ds.connection_id else None
        cp = db.query(IngestionCheckpoint).filter(IngestionCheckpoint.dataset_id == ds.id).first()
        sc = ds.sync_config or {}
        last = (db.query(IngestionRun).filter(IngestionRun.dataset_id == ds.id).order_by(IngestionRun.started_at.desc()).first())
        return _ok({"source": {
            "kind": ds.source_type if ds.source_type in ("database", "api", "object_storage") else "file",
            "connection": {"id": c.id, "name": c.name, "connector": c.connector, "status": c.status} if c else None,
            "table": ds.source_table, "mode": sc.get("mode"), "watermark_column": sc.get("watermark_column"),
            "primary_key": sc.get("primary_key") or [],
            "checkpoint": {"value": cp.value, "type": cp.column_type, "updated_at": _iso(cp.updated_at)} if cp else None,
            "last_run": {"id": last.id, "status": last.status, "mode": last.mode, "fallback_reason": last.fallback_reason} if last else None}})

    def _schedule_dict(s: DatasetSchedule) -> dict:
        return {"interval_minutes": s.interval_minutes, "mode": s.mode, "enabled": bool(s.enabled), "next_run_at": _iso(s.next_run_at),
                "last_run_at": _iso(s.last_run_at), "last_status": s.last_status, "consecutive_failures": s.consecutive_failures,
                "disabled_reason": s.disabled_reason}

    def _scheduled_dataset(db, user, dataset_id, min_role):
        ds = db.get(Dataset, dataset_id) if 0 < len(dataset_id or "") <= 64 else None
        try:
            if ds is None:
                raise ws_service.WorkspaceError("DATASET_NOT_FOUND", "Dataset not found.", 404)
            ws_service.require_role(db, user, ds.workspace_id, min_role)
        except ws_service.WorkspaceError as exc:
            raise ws_service.WorkspaceError("DATASET_NOT_FOUND", "Dataset not found.", 404) if exc.status_code == 404 else exc
        return ds

    @router.get("/datasets/{dataset_id}/schedule")
    def get_schedule(dataset_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ds = _scheduled_dataset(db, current_user, dataset_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _werr(exc)
        s = db.query(DatasetSchedule).filter(DatasetSchedule.dataset_id == ds.id).first()
        return _ok({"schedule": _schedule_dict(s) if s else None, "min_interval_minutes": config.SCHEDULER_MIN_INTERVAL_MINUTES,
                    "scheduler_in_process": config.SCHEDULER_ENABLED})

    @router.put("/datasets/{dataset_id}/schedule")
    @limiter.limit("20/minute")
    def set_schedule(request: Request, dataset_id: str, payload: ScheduleIn, current_user: User = Depends(get_current_user),
                     db: OrmSession = Depends(get_db)):
        """Sync a connection dataset every ``interval_minutes`` (analyst+). The first run is one interval from now; runs act as the
        caller. Syncs only happen while a scheduler process is running (see docs/OPERATIONS.md)."""
        try:
            ds = _scheduled_dataset(db, current_user, dataset_id, "analyst")
        except ws_service.WorkspaceError as exc:
            return _werr(exc)
        if not ds.connection_id or not ds.source_table:
            return _err("Only datasets that sync from a connection can be scheduled.", 409, "NOT_A_CONNECTION_DATASET")
        if payload.mode not in ("full_refresh", "incremental"):
            return _err("mode must be full_refresh or incremental.", 422, "INVALID_MODE")
        if not config.SCHEDULER_MIN_INTERVAL_MINUTES <= payload.interval_minutes <= 7 * 24 * 60:
            return _err(f"interval_minutes must be between {config.SCHEDULER_MIN_INTERVAL_MINUTES} and {7 * 24 * 60}.", 422, "INVALID_INTERVAL")
        now = datetime.now(timezone.utc)
        s = db.query(DatasetSchedule).filter(DatasetSchedule.dataset_id == ds.id).first()
        if s is None:
            s = DatasetSchedule(dataset_id=ds.id, interval_minutes=payload.interval_minutes, mode=payload.mode, enabled=payload.enabled,
                                next_run_at=now + timedelta(minutes=payload.interval_minutes), created_by=current_user.id)
            db.add(s)
        else:
            s.interval_minutes, s.mode, s.enabled, s.created_by = payload.interval_minutes, payload.mode, payload.enabled, current_user.id
            s.next_run_at, s.consecutive_failures, s.disabled_reason = now + timedelta(minutes=payload.interval_minutes), 0, None
        db.commit()
        audit.record(ds.workspace_id, "schedule.set", actor_id=current_user.id, target_type="dataset", target_id=ds.id,
                     details={"mode": payload.mode, "interval_minutes": payload.interval_minutes, "status": "enabled" if payload.enabled else "paused"})
        return _ok({"schedule": _schedule_dict(s)})

    @router.delete("/datasets/{dataset_id}/schedule")
    @limiter.limit("20/minute")
    def delete_schedule(request: Request, dataset_id: str, current_user: User = Depends(get_current_user),
                        db: OrmSession = Depends(get_db)):
        try:
            ds = _scheduled_dataset(db, current_user, dataset_id, "analyst")
        except ws_service.WorkspaceError as exc:
            return _werr(exc)
        removed = db.query(DatasetSchedule).filter(DatasetSchedule.dataset_id == ds.id).delete(synchronize_session=False)
        db.commit()
        if removed:
            audit.record(ds.workspace_id, "schedule.delete", actor_id=current_user.id, target_type="dataset", target_id=ds.id, details={})
        return _ok({"deleted": bool(removed)})

    return router
