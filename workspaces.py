"""Workspaces, membership roles and the dataset catalog (Sprint 1 foundation).

Authorization model
  * Every user has exactly one personal workspace (kind ``personal``) in which they are ``owner``; it cannot have other
    members. Team workspaces (kind ``team``) have one ``owner`` plus ``admin`` / ``analyst`` / ``viewer`` members.
  * Rank: viewer < analyst < admin < owner. ``require_role`` is the single authorization check for workspace routes; a
    non-member gets 404 (existence is not revealed), an insufficient role 403.
  * Role management: the owner may grant admin/analyst/viewer; an admin may only manage analyst/viewer members.
    Nobody changes their own role or the owner's; any non-owner may leave.

Dataset catalog
  * A ``datasets`` row describes one stored upload and exists exactly as long as the file: when storage releases a file
    (re-upload, flush, session delete) ``forget_storage_ref`` removes the row. Storage access itself is unchanged and
    still owner-scoped in storage.py: membership grants visibility of the catalog, not another user's file.
  * Invariant: a dataset always sits in a workspace its creator belongs to. Leaving / removal / workspace deletion moves
    the creator's datasets to their personal workspace instead of losing them.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import storage
import agent
import investigation
import assistant
import knowledge
from db import (Investigation, AgentRun, AuditLog, Connection, Conversation, Dataset, DatasetQualityReport, KnowledgeDocument, DatasetSchedule, IngestionCheckpoint, IngestionRun, PipelineSession, SessionLocal,
                User, Workspace, WorkspaceMember)

logger = logging.getLogger("querysure.workspaces")

ROLE_RANK = {"viewer": 10, "analyst": 20, "admin": 30, "owner": 40}
ASSIGNABLE_ROLES = ("admin", "analyst", "viewer")
MAX_OWNED_TEAM_WORKSPACES = 10
MAX_MEMBERS = 50
NAME_MAX = 120


class WorkspaceError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _not_found() -> WorkspaceError:
    return WorkspaceError("WORKSPACE_NOT_FOUND", "Workspace not found.", 404)


def _clean_name(name) -> str:
    value = " ".join(str(name or "").split())
    if not value:
        raise WorkspaceError("INVALID_NAME", "Workspace name is required.")
    if len(value) > NAME_MAX:
        raise WorkspaceError("INVALID_NAME", f"Workspace name must be at most {NAME_MAX} characters.")
    return value


# ---------------------------------------------------------------- personal workspace

def _find_personal(db: Session, user_id: str) -> Optional[Workspace]:
    return db.query(Workspace).filter(Workspace.personal_owner_id == user_id).first()


def personal_workspace_id(user_id: str) -> str:
    """Id of the user's personal workspace, creating it on first use. Runs in its own session so callers never have
    their transaction committed or rolled back; a concurrent creator loses on the unique constraint and re-reads."""
    db = SessionLocal()
    try:
        ws = _find_personal(db, user_id)
        if ws is not None:
            return ws.id
        ws = Workspace(name="Personal", kind="personal", owner_id=user_id, personal_owner_id=user_id)
        db.add(ws)
        db.flush()
        db.add(WorkspaceMember(workspace_id=ws.id, user_id=user_id, role="owner"))
        try:
            db.commit()
            return ws.id
        except IntegrityError:
            db.rollback()
            ws = _find_personal(db, user_id)
            if ws is None:
                raise
            return ws.id
    finally:
        db.close()


# ---------------------------------------------------------------- authorization

def membership(db: Session, workspace_id: str, user_id: str) -> Optional[WorkspaceMember]:
    return db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == workspace_id,
                                            WorkspaceMember.user_id == user_id).first()


def require_role(db: Session, user: User, workspace_id: str, min_role: str) -> tuple[Workspace, WorkspaceMember]:
    """The one authorization check for workspace-scoped operations."""
    if min_role not in ROLE_RANK:
        raise ValueError(f"unknown role {min_role}")
    if not workspace_id or not isinstance(workspace_id, str) or len(workspace_id) > 64:
        raise _not_found()
    member = membership(db, workspace_id, user.id)
    ws = db.get(Workspace, workspace_id) if member else None
    if member is None or ws is None:
        raise _not_found()
    if ROLE_RANK.get(member.role, 0) < ROLE_RANK[min_role]:
        raise WorkspaceError("FORBIDDEN", f"This action needs the {min_role} role or higher.", 403)
    return ws, member


def upload_workspace_id(db: Session, user: User, requested: Optional[str]) -> str:
    """Workspace a new upload is catalogued in: the requested one (analyst+ required) or the personal workspace."""
    if not requested:
        return personal_workspace_id(user.id)
    ws, _ = require_role(db, user, requested, "analyst")
    return ws.id


# ---------------------------------------------------------------- serialisation

def _emails(db: Session, user_ids) -> dict:
    ids = {u for u in user_ids if u}
    if not ids:
        return {}
    return dict(db.query(User.id, User.email).filter(User.id.in_(ids)).all())


def _iso(value):
    return value.isoformat() if value else None


def workspace_to_dict(db: Session, ws: Workspace, role: str) -> dict:
    members = db.query(func.count(WorkspaceMember.id)).filter(WorkspaceMember.workspace_id == ws.id).scalar() or 0
    datasets = db.query(func.count(Dataset.id)).filter(Dataset.workspace_id == ws.id).scalar() or 0
    return {"id": ws.id, "name": ws.name, "kind": ws.kind, "role": role, "member_count": members,
            "dataset_count": datasets, "created_at": _iso(ws.created_at)}


class DatasetAccessError(storage.DatasetUnavailable):
    """The session's dataset cannot be used right now; carries the API answer."""
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def session_db_path(db: Session, user: User, row: PipelineSession) -> str:
    """Local path of the database a session's questions run against. Workspace datasets are resolved through the catalog on
    EVERY call: membership (viewer+) is re-checked, so removing someone ends their access at once, and a re-import since the
    session was opened is refused until it is reopened. Legacy user-owned uploads resolve exactly as before."""
    ds = db.get(Dataset, row.dataset_id) if row.dataset_id else None
    if ds is None or not storage.is_workspace_ref(ds.storage_ref):
        return storage.ensure_local(row.db_path, user.id)
    if membership(db, ds.workspace_id, user.id) is None:
        raise DatasetAccessError("DATASET_ACCESS_REVOKED", "You no longer have access to this dataset.", 403)
    if ds.status != "ready":
        raise DatasetAccessError("DATASET_IMPORTING", "This dataset is being imported. Try again in a moment.", 409)
    if row.dataset_version != ds.version:
        raise DatasetAccessError("DATASET_CHANGED", "This dataset was re-imported since you opened it. Open it again from the "
                                 "Explore page to continue.", 409)
    return storage.ensure_local_workspace(ds.storage_ref)


def dataset_to_dict(ds: Dataset, emails: dict, viewer: User, viewer_role: str) -> dict:
    return {
        "status": ds.status, "version": ds.version, "connector": ds.connector, "source_type": ds.source_type,
        "source_table": ds.source_table, "syncs_from_connection": bool(ds.connection_id),
        "id": ds.id, "workspace_id": ds.workspace_id, "name": ds.name,
        "file_format": ds.file_format, "bytes": ds.bytes, "table_count": ds.table_count, "row_count": ds.row_count,
        "schema_version": ds.schema_version, "created_by": ds.created_by, "created_by_email": emails.get(ds.created_by),
        "created_at": _iso(ds.created_at), "updated_at": _iso(ds.updated_at),
        "can_delete": can_delete_dataset(ds, viewer, viewer_role),
    }


# ---------------------------------------------------------------- workspaces

def list_for_user(db: Session, user: User) -> list[dict]:
    personal_workspace_id(user.id)
    rows = (db.query(Workspace, WorkspaceMember.role)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .filter(WorkspaceMember.user_id == user.id)
            .order_by(Workspace.created_at).all())
    rows.sort(key=lambda r: r[0].kind != "personal")  # personal first, then team workspaces oldest first (stable sort)
    return [workspace_to_dict(db, ws, role) for ws, role in rows]


def create_team(db: Session, user: User, name) -> Workspace:
    name = _clean_name(name)
    owned = db.query(func.count(Workspace.id)).filter(Workspace.owner_id == user.id, Workspace.kind == "team").scalar() or 0
    if owned >= MAX_OWNED_TEAM_WORKSPACES:
        raise WorkspaceError("WORKSPACE_LIMIT", f"You can own at most {MAX_OWNED_TEAM_WORKSPACES} team workspaces.", 409)
    ws = Workspace(name=name, kind="team", owner_id=user.id)
    db.add(ws)
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
    db.commit()
    db.refresh(ws)
    return ws


def rename(db: Session, ws: Workspace, name) -> str:
    previous = ws.name
    ws.name = _clean_name(name)
    db.commit()
    return previous


def _rehome_datasets(db: Session, workspace_id: str, user_id: Optional[str] = None) -> int:
    """Move datasets out of ``workspace_id`` into each creator's personal workspace (no commit)."""
    q = db.query(Dataset).filter(Dataset.workspace_id == workspace_id)
    if user_id:
        q = q.filter(Dataset.created_by == user_id)
    moved = 0
    for ds in q.all():
        ds.workspace_id = personal_workspace_id(ds.created_by)
        moved += 1
    return moved


def _drop_connections(db: Session, workspace_id: str) -> int:
    """Delete a workspace's saved connections (and their encrypted secrets). Datasets imported from them keep their data, stop
    syncing, and lose their schedules. No commit."""
    ids = [c.id for c in db.query(Connection.id).filter(Connection.workspace_id == workspace_id).all()]
    if not ids:
        return 0
    detached = [d.id for d in db.query(Dataset.id).filter(Dataset.connection_id.in_(ids)).all()]
    if detached:
        db.query(DatasetSchedule).filter(DatasetSchedule.dataset_id.in_(detached)).delete(synchronize_session=False)
        db.query(Dataset).filter(Dataset.id.in_(detached)).update({Dataset.connection_id: None}, synchronize_session=False)
    db.query(Connection).filter(Connection.id.in_(ids)).delete(synchronize_session=False)
    return len(ids)


def delete_team(db: Session, ws: Workspace) -> int:
    """Delete a team workspace: datasets go back to their creators' personal workspaces; members and the workspace's
    audit trail are erased with it. Returns the number of datasets re-homed. Caller checked owner role."""
    if ws.kind != "team":
        raise WorkspaceError("PERSONAL_WORKSPACE", "A personal workspace cannot be deleted.", 400)
    _drop_connections(db, ws.id)
    knowledge.delete_documents(db, [d.id for d in db.query(KnowledgeDocument.id).filter(KnowledgeDocument.workspace_id == ws.id)])
    assistant.delete_conversations(db, [c.id for c in db.query(Conversation.id).filter(Conversation.workspace_id == ws.id)])
    agent.delete_runs(db, [r.id for r in db.query(AgentRun.id).filter(AgentRun.workspace_id == ws.id)])
    moved = _rehome_datasets(db, ws.id)
    db.flush()
    db.query(AuditLog).filter(AuditLog.workspace_id == ws.id).delete(synchronize_session=False)
    db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == ws.id).delete(synchronize_session=False)
    db.query(Workspace).filter(Workspace.id == ws.id).delete(synchronize_session=False)
    db.commit()
    return moved


# ---------------------------------------------------------------- members

def list_members(db: Session, ws: Workspace) -> list[dict]:
    rows = (db.query(WorkspaceMember, User.email)
            .join(User, User.id == WorkspaceMember.user_id)
            .filter(WorkspaceMember.workspace_id == ws.id)
            .order_by(WorkspaceMember.created_at).all())
    order = {"owner": 0, "admin": 1, "analyst": 2, "viewer": 3}
    out = [{"user_id": m.user_id, "email": email, "role": m.role, "joined_at": _iso(m.created_at)} for m, email in rows]
    return sorted(out, key=lambda r: order.get(r["role"], 9))


def _check_manage(actor: WorkspaceMember, target_role: Optional[str], new_role: Optional[str]) -> None:
    """Owner manages admin/analyst/viewer; admin manages analyst/viewer only."""
    if actor.role == "owner":
        return
    if actor.role != "admin":
        raise WorkspaceError("FORBIDDEN", "Only workspace owners and admins can manage members.", 403)
    for role in (target_role, new_role):
        if role in ("owner", "admin"):
            raise WorkspaceError("FORBIDDEN", "Only the workspace owner can manage admins.", 403)


def add_member(db: Session, ws: Workspace, actor: WorkspaceMember, email, role) -> WorkspaceMember:
    if ws.kind != "team":
        raise WorkspaceError("PERSONAL_WORKSPACE", "Create a team workspace to add members.", 400)
    if role not in ASSIGNABLE_ROLES:
        raise WorkspaceError("INVALID_ROLE", "Role must be admin, analyst or viewer.")
    _check_manage(actor, None, role)
    email = str(email or "").strip().lower()
    if not email or "@" not in email or len(email) > 320:
        raise WorkspaceError("INVALID_EMAIL", "Enter the e-mail address of an existing QuerySure account.")
    target = db.query(User).filter(func.lower(User.email) == email).first()
    if target is None or not target.is_active or not target.email_verified:
        raise WorkspaceError("USER_NOT_FOUND", "No verified QuerySure account uses that e-mail address.", 404)
    if membership(db, ws.id, target.id):
        raise WorkspaceError("ALREADY_MEMBER", "That account is already a member of this workspace.", 409)
    count = db.query(func.count(WorkspaceMember.id)).filter(WorkspaceMember.workspace_id == ws.id).scalar() or 0
    if count >= MAX_MEMBERS:
        raise WorkspaceError("MEMBER_LIMIT", f"A workspace can have at most {MAX_MEMBERS} members.", 409)
    member = WorkspaceMember(workspace_id=ws.id, user_id=target.id, role=role)
    db.add(member)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise WorkspaceError("ALREADY_MEMBER", "That account is already a member of this workspace.", 409)
    db.refresh(member)
    return member


def change_role(db: Session, ws: Workspace, actor: WorkspaceMember, target_user_id: str, role) -> tuple[WorkspaceMember, str]:
    if role not in ASSIGNABLE_ROLES:
        raise WorkspaceError("INVALID_ROLE", "Role must be admin, analyst or viewer.")
    target = membership(db, ws.id, target_user_id)
    if target is None:
        raise WorkspaceError("MEMBER_NOT_FOUND", "Member not found.", 404)
    if target.user_id == actor.user_id:
        raise WorkspaceError("FORBIDDEN", "You cannot change your own role.", 403)
    if target.role == "owner":
        raise WorkspaceError("FORBIDDEN", "The owner's role cannot be changed.", 403)
    _check_manage(actor, target.role, role)
    previous = target.role
    target.role = role
    db.commit()
    db.refresh(target)
    return target, previous


def remove_member(db: Session, ws: Workspace, actor: WorkspaceMember, target_user_id: str) -> tuple[str, int]:
    """Remove a member (or leave, when target is the actor). Their datasets here return to their personal workspace.
    Returns (removed role, datasets re-homed)."""
    target = membership(db, ws.id, target_user_id)
    if target is None:
        raise WorkspaceError("MEMBER_NOT_FOUND", "Member not found.", 404)
    if target.role == "owner":
        raise WorkspaceError("FORBIDDEN", "The owner cannot leave or be removed; delete the workspace instead.", 403)
    if target.user_id != actor.user_id:
        _check_manage(actor, target.role, None)
    role = target.role
    moved = _rehome_datasets(db, ws.id, target.user_id)
    db.delete(target)
    db.commit()
    return role, moved


# ---------------------------------------------------------------- datasets

def list_datasets(db: Session, ws: Workspace, viewer: User, viewer_role: str) -> list[dict]:
    rows = db.query(Dataset).filter(Dataset.workspace_id == ws.id).order_by(Dataset.created_at.desc()).limit(500).all()
    emails = _emails(db, [r.created_by for r in rows])
    import quality
    q = quality.latest_status(db, [r.id for r in rows])
    return [{**dataset_to_dict(r, emails, viewer, viewer_role), "quality": q.get(r.id)} for r in rows]


def can_delete_dataset(ds: Dataset, user: User, role: str) -> bool:
    rank = ROLE_RANK.get(role, 0)
    return rank >= ROLE_RANK["admin"] or (ds.created_by == user.id and rank >= ROLE_RANK["analyst"])


def register_dataset(db: Session, *, user_id: str, workspace_id: Optional[str], storage_ref: str, name: str,
                     file_format: Optional[str], size: Optional[int], schema: Optional[dict]) -> Dataset:
    """Catalog a freshly stored upload (idempotent per storage_ref, so a retried job reuses its row). Falls back to the
    personal workspace when the creator is no longer analyst+ in the requested one."""
    existing = db.query(Dataset).filter(Dataset.storage_ref == storage_ref).first()
    if existing is not None:
        return existing
    target = workspace_id
    if target:
        member = membership(db, target, user_id)
        if member is None or ROLE_RANK.get(member.role, 0) < ROLE_RANK["analyst"]:
            target = None
    if not target:
        target = personal_workspace_id(user_id)
    schema = schema if isinstance(schema, dict) else {}
    ds = Dataset(workspace_id=target, created_by=user_id, name=(name or "Uploaded dataset")[:255], source_type="upload",
                 file_format=file_format, storage_ref=storage_ref, bytes=size, table_count=schema.get("table_count"),
                 row_count=schema.get("total_rows"))
    db.add(ds)
    db.commit()
    db.refresh(ds)
    return ds


def discard_unlinked_dataset(db: Session, dataset_id: str) -> bool:
    """Delete a catalog row that no session points at (an upload job failed after registering it). The stored file is
    left alone: the job keeps its reference for a retry, as before. Never raises."""
    try:
        db.rollback()
        if db.query(PipelineSession.id).filter(PipelineSession.dataset_id == dataset_id).first():
            return False
        removed = db.query(Dataset).filter(Dataset.id == dataset_id).delete(synchronize_session=False)
        db.commit()
        return bool(removed)
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask the job's real error
        db.rollback()
        logger.warning("unlinked dataset cleanup failed: %s", type(exc).__name__)
        return False


def forget_storage_ref(db: Session, ref: Optional[str]) -> int:
    """The file behind ``ref`` was deleted from storage: drop its catalog row and unlink sessions. Never raises."""
    if not ref:
        return 0
    try:
        ids = [d.id for d in db.query(Dataset.id).filter(Dataset.storage_ref == ref).all()]
        if not ids:
            return 0
        db.query(PipelineSession).filter(PipelineSession.dataset_id.in_(ids)).update(
            {PipelineSession.dataset_id: None}, synchronize_session=False)
        db.query(Dataset).filter(Dataset.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        return len(ids)
    except Exception as exc:  # noqa: BLE001 - catalog cleanup must not fail the request that released the file
        db.rollback()
        logger.warning("dataset catalog cleanup failed: %s", type(exc).__name__)
        return 0


def delete_dataset(db: Session, ds: Dataset) -> list[str]:
    """Delete a dataset everywhere: the stored file first (fail-closed: nothing changes if storage refuses), then every
    session that points at it is emptied like a flush, then the catalog row. Returns the emptied session ids."""
    from account_service import _delete_dataset_strict
    from db import SESSION_FIELDS
    ref = ds.storage_ref
    try:
        if not ref.startswith("pending:"):
            _delete_dataset_strict(ref)
    except Exception as exc:  # noqa: BLE001
        logger.error("dataset delete aborted: storage failure (%s)", type(exc).__name__)
        raise WorkspaceError("DELETE_INCOMPLETE", "The stored file could not be removed. Nothing was deleted; please retry.", 503) from exc
    sessions = db.query(PipelineSession).filter((PipelineSession.db_path == ref) | (PipelineSession.dataset_id == ds.id)).all()
    db.query(DatasetQualityReport).filter(DatasetQualityReport.dataset_id == ds.id).delete(synchronize_session=False)
    investigation.delete_investigations(db, Investigation.dataset_id == ds.id)
    db.query(IngestionRun).filter(IngestionRun.dataset_id == ds.id).delete(synchronize_session=False)
    db.query(IngestionCheckpoint).filter(IngestionCheckpoint.dataset_id == ds.id).delete(synchronize_session=False)
    db.query(DatasetSchedule).filter(DatasetSchedule.dataset_id == ds.id).delete(synchronize_session=False)
    cleared = {field: None for field in SESSION_FIELDS}
    for row in sessions:
        for field, value in cleared.items():
            setattr(row, field, value)
        row.dataset_bytes = None
        row.dataset_id = None
        row.dataset_version = None
        row.version = (row.version or 0) + 1
    db.query(Dataset).filter(Dataset.id == ds.id).delete(synchronize_session=False)
    db.commit()
    return [s.id for s in sessions]


# ---------------------------------------------------------------- account erasure

def erase_user(db: Session, user_id: str) -> None:
    """Remove the user's workspace footprint inside the caller's transaction (no commit); call it before the caller's
    other writes. Team workspaces they own are deleted with other members' datasets re-homed; their own datasets' files
    were already removed by the caller."""
    # Re-home first, before this transaction writes anything: personal_workspace_id may insert in its own session, and
    # SQLite allows one writer at a time.
    for ws in db.query(Workspace).filter(Workspace.owner_id == user_id, Workspace.kind == "team").all():
        for ds in db.query(Dataset).filter(Dataset.workspace_id == ws.id, Dataset.created_by != user_id).all():
            ds.workspace_id = personal_workspace_id(ds.created_by)
    db.flush()
    owned = [w.id for w in db.query(Workspace.id).filter(Workspace.owner_id == user_id).all()]
    for ws_id in owned:
        _drop_connections(db, ws_id)
    knowledge.delete_documents(db, [d.id for d in db.query(KnowledgeDocument.id).filter(
        (KnowledgeDocument.created_by == user_id) | (KnowledgeDocument.workspace_id.in_(owned or [""])))])
    assistant.delete_conversations(db, [c.id for c in db.query(Conversation.id).filter(
        (Conversation.user_id == user_id) | (Conversation.workspace_id.in_(owned or [""])))])
    agent.delete_runs(db, [r.id for r in db.query(AgentRun.id).filter((AgentRun.user_id == user_id) | (AgentRun.workspace_id.in_(owned or [""])))])
    mine = [d.id for d in db.query(Dataset.id).filter(Dataset.created_by == user_id).all()]
    if mine:
        db.query(DatasetQualityReport).filter(DatasetQualityReport.dataset_id.in_(mine)).delete(synchronize_session=False)
        investigation.delete_investigations(db, Investigation.dataset_id.in_(mine))
        db.query(IngestionRun).filter(IngestionRun.dataset_id.in_(mine)).delete(synchronize_session=False)
        db.query(IngestionCheckpoint).filter(IngestionCheckpoint.dataset_id.in_(mine)).delete(synchronize_session=False)
        db.query(DatasetSchedule).filter(DatasetSchedule.dataset_id.in_(mine)).delete(synchronize_session=False)
    db.query(Dataset).filter(Dataset.created_by == user_id).delete(synchronize_session=False)
    if owned:
        db.query(Dataset).filter(Dataset.workspace_id.in_(owned)).delete(synchronize_session=False)
        db.query(AuditLog).filter(AuditLog.workspace_id.in_(owned)).delete(synchronize_session=False)
        db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id.in_(owned)).delete(synchronize_session=False)
    db.query(WorkspaceMember).filter(WorkspaceMember.user_id == user_id).delete(synchronize_session=False)
    if owned:
        db.query(Workspace).filter(Workspace.id.in_(owned)).delete(synchronize_session=False)
