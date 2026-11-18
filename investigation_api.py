"""Investigation routes (Sprint 10). See ``investigation.py`` for the fact / hypothesis separation.

Roles: viewer+ lists options and reads investigations of the workspace; analyst+ runs one; admin (or the creator at analyst+) deletes.
A non-member gets 404 for the dataset and for every investigation id. Exports (JSON, HTML) reuse ``report_storage`` and the existing
owner-only ``GET /api/reports/{id}/download``.
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as OrmSession

import audit
import config
import investigation
import knowledge_api
import report_storage
import storage
import workspaces as ws_service
from auth import get_current_user
from db import Dataset, Investigation, Report, User, get_db

logger = logging.getLogger("querysure.investigation")
_ok, _err = knowledge_api._ok, knowledge_api._err


class InvestigationIn(BaseModel):
    kind: str = Field(..., max_length=16)
    table: str = Field(..., max_length=255)
    measure: Optional[str] = Field(default=None, max_length=255)
    date_column: Optional[str] = Field(default=None, max_length=255)
    dimension: Optional[str] = Field(default=None, max_length=255)
    agg: Optional[str] = Field(default=None, max_length=8)
    split_at: Optional[str] = Field(default=None, max_length=7)


def _to_dict(inv: Investigation, full: bool) -> dict:
    d = {"id": inv.id, "workspace_id": inv.workspace_id, "dataset_id": inv.dataset_id, "kind": inv.kind, "title": inv.title,
         "dataset_version": inv.dataset_version, "created_by": inv.created_by, "params": inv.params, "exports": inv.exports or {},
         "created_at": inv.created_at.isoformat() if inv.created_at else None}
    if full:
        d["report"] = inv.report
    return d


def build_router(limiter) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["investigations"])

    def _wserr(exc):
        return _err(exc.message, exc.status_code, exc.code)

    def _dataset(db, user, dataset_id, role):
        ds = db.get(Dataset, dataset_id) if 0 < len(dataset_id or "") <= 64 else None
        try:
            if ds is None:
                raise ws_service.WorkspaceError("DATASET_NOT_FOUND", "Dataset not found.", 404)
            _, member = ws_service.require_role(db, user, ds.workspace_id, role)
        except ws_service.WorkspaceError as exc:
            raise ws_service.WorkspaceError("DATASET_NOT_FOUND", "Dataset not found.", 404) if exc.status_code == 404 else exc
        return ds, member

    def _local(ds):
        if ds.status != "ready":
            raise investigation.InvestigationError("DATASET_NOT_READY", "The dataset is still importing.", 409)
        try:
            return storage.ensure_local_workspace(ds.storage_ref)
        except Exception as exc:  # noqa: BLE001
            raise investigation.InvestigationError("DATASET_UNAVAILABLE", "The dataset file is not available.", 410) from exc

    @router.get("/datasets/{dataset_id}/investigation-options")
    def options(dataset_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ds, _ = _dataset(db, current_user, dataset_id, "viewer")
            schema = investigation.introspect(_local(ds))
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        except investigation.InvestigationError as exc:
            return _err(exc.message, exc.status, exc.code)
        return _ok({"dataset": {"id": ds.id, "name": ds.name, "version": ds.version}, "kinds": list(investigation.KINDS), "aggs": list(investigation.AGGS),
                    "tables": [{"name": t, "rows": v["rows"], "columns": v["columns"]} for t, v in schema.items()]})

    @router.post("/datasets/{dataset_id}/investigations")
    @limiter.limit("10/minute")
    def create(request: Request, dataset_id: str, payload: InvestigationIn, current_user: User = Depends(get_current_user),
               db: OrmSession = Depends(get_db)):
        try:
            ds, _ = _dataset(db, current_user, dataset_id, "analyst")
            report = investigation.run(db, ds, _local(ds), payload.model_dump(exclude_none=True))
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        except investigation.InvestigationError as exc:
            return _err(exc.message, exc.status, exc.code)
        inv = Investigation(workspace_id=ds.workspace_id, dataset_id=ds.id, created_by=current_user.id, kind=report["kind"], title=report["title"][:255],
                            dataset_version=ds.version, params=report["params"], report=report, exports={})
        db.add(inv)
        db.flush()
        old = db.query(Investigation.id).filter(Investigation.dataset_id == ds.id).order_by(Investigation.created_at.desc()).offset(config.INVESTIGATIONS_KEEP).all()
        if old:
            investigation.delete_investigations(db, Investigation.id.in_([o.id for o in old]))
        db.commit()
        export_error = None
        try:     # exports are best effort and reported honestly: the investigation itself is already saved
            created = {}
            with tempfile.TemporaryDirectory(prefix="qs-inv-") as tmp:
                for kind, body in (("json", investigation.dumps(report)), ("html", investigation.render_html(report))):
                    p = os.path.join(tmp, f"investigation.{kind}")
                    with open(p, "w", encoding="utf-8") as fh:
                        fh.write(body)
                    key, size = report_storage.store_report_file(p, current_user.id, kind)
                    row = Report(user_id=current_user.id, session_id=None, key=key, kind=kind, size=size)
                    db.add(row)
                    db.flush()
                    created[kind] = row.id
            inv.exports = created
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            export_error = "EXPORT_FAILED"
            logger.warning("investigation export failed: %s", type(exc).__name__)
        audit.record(ds.workspace_id, "investigation.create", actor_id=current_user.id, target_type="investigation", target_id=inv.id,
                     details={"kind": inv.kind, "dataset_id": ds.id, "facts": len(report["facts"])})
        db.refresh(inv)
        return _ok({"investigation": _to_dict(inv, True), **({"export_error": export_error} if export_error else {})}, 201)

    @router.get("/workspaces/{workspace_id}/investigations")
    def list_investigations(workspace_id: str, dataset_id: Optional[str] = None, current_user: User = Depends(get_current_user),
                            db: OrmSession = Depends(get_db)):
        try:
            ws, _ = ws_service.require_role(db, current_user, workspace_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        q = db.query(Investigation).filter(Investigation.workspace_id == ws.id)
        if dataset_id:
            q = q.filter(Investigation.dataset_id == dataset_id)
        return _ok({"investigations": [_to_dict(i, False) for i in q.order_by(Investigation.created_at.desc()).limit(100).all()]})

    def _load(db, user, investigation_id, role):
        inv = db.get(Investigation, investigation_id) if 0 < len(investigation_id or "") <= 64 else None
        try:
            if inv is None:
                raise ws_service.WorkspaceError("INVESTIGATION_NOT_FOUND", "Investigation not found.", 404)
            _, member = ws_service.require_role(db, user, inv.workspace_id, role)
        except ws_service.WorkspaceError as exc:
            raise ws_service.WorkspaceError("INVESTIGATION_NOT_FOUND", "Investigation not found.", 404) if exc.status_code == 404 else exc
        return inv, member

    @router.get("/investigations/{investigation_id}")
    def get_one(investigation_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            inv, _ = _load(db, current_user, investigation_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        return _ok({"investigation": _to_dict(inv, True)})

    @router.delete("/investigations/{investigation_id}")
    def delete_one(investigation_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            inv, member = _load(db, current_user, investigation_id, "analyst")
            if inv.created_by != current_user.id and ws_service.ROLE_RANK[member.role] < ws_service.ROLE_RANK["admin"]:
                raise ws_service.WorkspaceError("FORBIDDEN", "Only the creator or a workspace admin can delete this investigation.", 403)
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        ws_id = inv.workspace_id
        investigation.delete_investigations(db, Investigation.id == inv.id)
        db.commit()
        audit.record(ws_id, "investigation.delete", actor_id=current_user.id, target_type="investigation", target_id=investigation_id)
        return _ok({"deleted": True})

    return router
