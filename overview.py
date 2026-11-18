"""Workspace overview (Sprint 12): one read-only summary of what needs attention, built from records the platform already keeps.
Everything is scoped to one workspace and to what the caller may see: agent approvals are the caller's own runs (all runs for admin+),
conversations are the caller's own, and AI usage is admin+ only."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func

import governance
import quality
from db import AgentRun, AgentStep, Conversation, Dataset, DatasetSchedule, IngestionRun, Investigation, KnowledgeChunk, KnowledgeDocument

ADMIN_RANK = 30


def build(db, ws, user, role: str, rank: int) -> dict:
    datasets = db.query(Dataset).filter(Dataset.workspace_id == ws.id).order_by(Dataset.created_at.desc()).limit(500).all()
    ids = [d.id for d in datasets]
    q = quality.latest_status(db, ids)
    names = {d.id: d.name for d in datasets}
    by_status, by_quality, attention = {}, {"ok": 0, "warn": 0, "error": 0, "none": 0}, []
    for d in datasets:
        by_status[d.status] = by_status.get(d.status, 0) + 1
        qs = (q.get(d.id) or {}).get("status")
        by_quality[qs if qs in by_quality else "none"] += 1
        if d.status != "ready" or qs in ("warn", "error"):
            attention.append({"dataset_id": d.id, "name": d.name, "status": d.status, "quality": qs, "findings": (q.get(d.id) or {}).get("findings", 0)})
    runs = db.query(IngestionRun).filter(IngestionRun.workspace_id == ws.id).order_by(IngestionRun.started_at.desc()).limit(5).all()
    sched = db.query(DatasetSchedule).filter(DatasetSchedule.dataset_id.in_(ids or [""])).all()
    failed_sched = [s for s in sched if s.last_status == "failed"]
    docs = db.query(func.count(KnowledgeDocument.id)).filter(KnowledgeDocument.workspace_id == ws.id).scalar() or 0
    chunks = db.query(func.count(KnowledgeChunk.id)).filter(KnowledgeChunk.workspace_id == ws.id).scalar() or 0
    runs_q = db.query(AgentRun.id, AgentRun.goal, AgentRun.user_id).filter(AgentRun.workspace_id == ws.id, AgentRun.status == "awaiting_approval")
    if rank < ADMIN_RANK:
        runs_q = runs_q.filter(AgentRun.user_id == user.id)
    waiting = runs_q.order_by(AgentRun.created_at.desc()).limit(10).all()
    pending = []
    if waiting:
        by_run = {r.id: r for r in waiting}
        for s in db.query(AgentStep).filter(AgentStep.run_id.in_(list(by_run)), AgentStep.status == "awaiting_approval").all():
            pending.append({"run_id": s.run_id, "step_id": s.id, "tool": s.tool, "goal": by_run[s.run_id].goal[:120]})
    invs = db.query(Investigation).filter(Investigation.workspace_id == ws.id).order_by(Investigation.created_at.desc()).limit(5).all()
    out = {
        "workspace": {"id": ws.id, "name": ws.name, "kind": ws.kind, "role": role},
        "datasets": {"total": len(datasets), "by_status": by_status, "by_quality": by_quality, "attention": attention[:10]},
        "ingestion": {"recent_runs": [{"dataset": names.get(r.dataset_id), "mode": r.mode, "status": r.status, "records_inserted": int(r.records_inserted or 0),
                                       "at": r.started_at.isoformat() if r.started_at else None} for r in runs],
                      "schedules": {"enabled": sum(1 for s in sched if s.enabled), "disabled": sum(1 for s in sched if not s.enabled), "last_failed": len(failed_sched)}},
        "knowledge": {"documents": int(docs), "passages": int(chunks)},
        "conversations": {"mine": db.query(func.count(Conversation.id)).filter(Conversation.workspace_id == ws.id, Conversation.user_id == user.id).scalar() or 0},
        "agent": {"awaiting_approval": pending},
        "investigations": {"recent": [{"id": i.id, "title": i.title, "dataset_version": i.dataset_version,
                                       "at": i.created_at.isoformat() if i.created_at else None} for i in invs]},
        "ai": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if rank >= ADMIN_RANK:
        u = governance.usage(db, ws.id, 7)
        calls = sum(t["calls"] for t in u["by_task"].values())
        fails = sum(t["failures"] for t in u["by_task"].values())
        out["ai"] = {"window_days": 7, "operations": u["operations"], "calls": calls, "failures": fails, "total_cost_cents": u["total_cost_cents"]}
    out["attention_count"] = len(attention) + len(pending) + len(failed_sched)
    return out

