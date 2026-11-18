"""Workspace, membership, dataset-catalog and audit-trail routes (Sprint 1).

Every route authenticates with ``get_current_user`` and authorizes with ``workspaces.require_role`` -- the single
check -- so a non-member always gets 404 and an insufficient role 403. Responses never include storage references,
password hashes or other users' data beyond the e-mail addresses of members of a workspace the caller belongs to.
Mutations are rate limited and audited. Built by ``build_router(limiter)`` so the app's limiter is shared without an
import cycle with api.py.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as OrmSession

import audit
import workspaces as ws_service
from auth import get_current_user
from db import AuditLog, Dataset, User, get_db

AUDIT_PAGE_MAX = 100


class WorkspaceIn(BaseModel):
    name: str = Field(..., max_length=500)


class MemberIn(BaseModel):
    email: str = Field(..., max_length=320)
    role: str = Field(..., max_length=16)


class RoleIn(BaseModel):
    role: str = Field(..., max_length=16)


def _ok(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content={"success": True, **data})


def _err(exc: ws_service.WorkspaceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"success": False, "error": exc.message, "error_code": exc.code})


def build_router(limiter) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["workspaces"])

    @router.get("/workspaces")
    def list_workspaces(current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        return _ok({"workspaces": ws_service.list_for_user(db, current_user),
                    "personal_workspace_id": ws_service.personal_workspace_id(current_user.id)})

    @router.post("/workspaces")
    @limiter.limit("10/minute")
    def create_workspace(request: Request, payload: WorkspaceIn = Body(...),
                         current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws = ws_service.create_team(db, current_user, payload.name)
        except ws_service.WorkspaceError as exc:
            return _err(exc)
        audit.record(ws.id, "workspace.create", actor_id=current_user.id, target_type="workspace", target_id=ws.id,
                     details={"name": ws.name, "workspace_kind": ws.kind})
        return _ok({"workspace": ws_service.workspace_to_dict(db, ws, "owner")}, 201)

    @router.get("/workspaces/{workspace_id}")
    def get_workspace(workspace_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws, member = ws_service.require_role(db, current_user, workspace_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _err(exc)
        return _ok({"workspace": ws_service.workspace_to_dict(db, ws, member.role)})

    @router.patch("/workspaces/{workspace_id}")
    @limiter.limit("20/minute")
    def rename_workspace(request: Request, workspace_id: str, payload: WorkspaceIn = Body(...),
                         current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws, member = ws_service.require_role(db, current_user, workspace_id, "admin")
            previous = ws_service.rename(db, ws, payload.name)
        except ws_service.WorkspaceError as exc:
            return _err(exc)
        audit.record(ws.id, "workspace.rename", actor_id=current_user.id, target_type="workspace", target_id=ws.id,
                     details={"name": ws.name, "previous_name": previous})
        return _ok({"workspace": ws_service.workspace_to_dict(db, ws, member.role)})

    @router.delete("/workspaces/{workspace_id}")
    @limiter.limit("10/minute")
    def delete_workspace(request: Request, workspace_id: str,
                         current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws, _ = ws_service.require_role(db, current_user, workspace_id, "owner")
            name = ws.name
            moved = ws_service.delete_team(db, ws)
        except ws_service.WorkspaceError as exc:
            return _err(exc)
        # The workspace's own trail is erased with it; the deletion is recorded in the owner's personal workspace.
        audit.record_for_user(current_user.id, "workspace.delete", target_type="workspace", target_id=workspace_id,
                              details={"name": name, "rehomed": moved})
        return _ok({"deleted": True, "workspace_id": workspace_id, "datasets_rehomed": moved})

    @router.get("/workspaces/{workspace_id}/members")
    def list_members(workspace_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws, member = ws_service.require_role(db, current_user, workspace_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _err(exc)
        return _ok({"members": ws_service.list_members(db, ws), "your_role": member.role})

    @router.post("/workspaces/{workspace_id}/members")
    @limiter.limit("10/minute")
    def add_member(request: Request, workspace_id: str, payload: MemberIn = Body(...),
                   current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws, actor = ws_service.require_role(db, current_user, workspace_id, "admin")
            member = ws_service.add_member(db, ws, actor, payload.email, payload.role)
        except ws_service.WorkspaceError as exc:
            return _err(exc)
        audit.record(ws.id, "member.add", actor_id=current_user.id, target_type="user", target_id=member.user_id,
                     details={"role": member.role})
        return _ok({"members": ws_service.list_members(db, ws)}, 201)

    @router.patch("/workspaces/{workspace_id}/members/{user_id}")
    @limiter.limit("20/minute")
    def change_member_role(request: Request, workspace_id: str, user_id: str, payload: RoleIn = Body(...),
                           current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws, actor = ws_service.require_role(db, current_user, workspace_id, "admin")
            member, previous = ws_service.change_role(db, ws, actor, user_id, payload.role)
        except ws_service.WorkspaceError as exc:
            return _err(exc)
        audit.record(ws.id, "member.role_change", actor_id=current_user.id, target_type="user", target_id=user_id,
                     details={"role": member.role, "previous_role": previous})
        return _ok({"members": ws_service.list_members(db, ws)})

    @router.delete("/workspaces/{workspace_id}/members/{user_id}")
    @limiter.limit("20/minute")
    def remove_member(request: Request, workspace_id: str, user_id: str,
                      current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws, actor = ws_service.require_role(db, current_user, workspace_id, "viewer")
            if user_id != current_user.id and ws_service.ROLE_RANK[actor.role] < ws_service.ROLE_RANK["admin"]:
                raise ws_service.WorkspaceError("FORBIDDEN", "Only workspace owners and admins can remove members.", 403)
            role, moved = ws_service.remove_member(db, ws, actor, user_id)
        except ws_service.WorkspaceError as exc:
            return _err(exc)
        action = "member.leave" if user_id == current_user.id else "member.remove"
        audit.record(ws.id, action, actor_id=current_user.id, target_type="user", target_id=user_id,
                     details={"role": role, "rehomed": moved})
        return _ok({"removed": True, "user_id": user_id, "datasets_rehomed": moved})

    @router.get("/workspaces/{workspace_id}/datasets")
    def list_datasets(workspace_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws, member = ws_service.require_role(db, current_user, workspace_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _err(exc)
        return _ok({"datasets": ws_service.list_datasets(db, ws, current_user, member.role), "your_role": member.role})

    @router.delete("/datasets/{dataset_id}")
    @limiter.limit("20/minute")
    def delete_dataset(request: Request, dataset_id: str,
                       current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        ds = db.get(Dataset, dataset_id) if len(dataset_id) <= 64 else None
        not_found = ws_service.WorkspaceError("DATASET_NOT_FOUND", "Dataset not found.", 404)
        if ds is None:
            return _err(not_found)
        try:
            ws, member = ws_service.require_role(db, current_user, ds.workspace_id, "viewer")
        except ws_service.WorkspaceError:
            return _err(not_found)  # not a member: do not reveal that the dataset exists
        if not ws_service.can_delete_dataset(ds, current_user, member.role):
            return _err(ws_service.WorkspaceError("FORBIDDEN", "Only the dataset's creator (analyst or above) or a workspace admin can delete it.", 403))
        info = {"format": ds.file_format, "bytes": ds.bytes}  # by id only: the file name may be personal
        try:
            emptied = ws_service.delete_dataset(db, ds)
        except ws_service.WorkspaceError as exc:
            return _err(exc)
        try:
            from modules import clear_cache_key
            for sid in emptied:
                clear_cache_key(sid)
        except Exception:  # noqa: BLE001 - in-process cache only
            pass
        audit.record(ws.id, "dataset.delete", actor_id=current_user.id, target_type="dataset", target_id=dataset_id,
                     details={**info, "count": len(emptied)})
        return _ok({"deleted": True, "dataset_id": dataset_id, "sessions_cleared": len(emptied)})

    @router.get("/workspaces/{workspace_id}/audit")
    def audit_trail(workspace_id: str, limit: int = 50, before: Optional[str] = None,
                    current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws, _ = ws_service.require_role(db, current_user, workspace_id, "admin")
        except ws_service.WorkspaceError as exc:
            return _err(exc)
        limit = max(1, min(int(limit or 50), AUDIT_PAGE_MAX))
        q = db.query(AuditLog).filter(AuditLog.workspace_id == ws.id)
        if before:
            anchor = db.get(AuditLog, before) if len(before) <= 64 else None
            if anchor is None or anchor.workspace_id != ws.id:
                return _err(ws_service.WorkspaceError("INVALID_CURSOR", "Unknown page cursor."))
            q = q.filter((AuditLog.created_at < anchor.created_at)
                         | ((AuditLog.created_at == anchor.created_at) & (AuditLog.id < anchor.id)))
        rows = q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit + 1).all()
        more = len(rows) > limit
        rows = rows[:limit]
        emails = ws_service._emails(db, [r.actor_id for r in rows])
        return _ok({
            "entries": [{
                "id": r.id, "created_at": r.created_at.isoformat() if r.created_at else None,
                "actor_type": r.actor_type, "actor_id": r.actor_id, "actor_email": emails.get(r.actor_id),
                "action": r.action, "target_type": r.target_type, "target_id": r.target_id,
                "details": r.details or {}, "request_id": r.request_id,
            } for r in rows],
            "next_cursor": rows[-1].id if more and rows else None,
        })

    return router
