"""Governance and dashboard routes: the AI usage report (Sprint 11, admin+ only: it shows who spent what) and the workspace overview (Sprint 12)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as OrmSession

import governance
import overview
import knowledge_api
import workspaces as ws_service
from auth import get_current_user
from db import User, get_db

_ok, _err = knowledge_api._ok, knowledge_api._err


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["governance"])

    @router.get("/workspaces/{workspace_id}/ai-usage")
    def ai_usage(workspace_id: str, days: int = 30, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws, _ = ws_service.require_role(db, current_user, workspace_id, "admin")
        except ws_service.WorkspaceError as exc:
            return _err(exc.message, exc.status_code, exc.code)
        return _ok({"usage": governance.usage(db, ws.id, max(1, min(int(days or 30), 365)))})

    @router.get("/workspaces/{workspace_id}/overview")
    def workspace_overview(workspace_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws, member = ws_service.require_role(db, current_user, workspace_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _err(exc.message, exc.status_code, exc.code)
        return _ok({"overview": overview.build(db, ws, current_user, member.role, ws_service.ROLE_RANK[member.role])})

    return router
