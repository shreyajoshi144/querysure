"""AI governance report (Sprint 11): what the AI features of one workspace did, from the records the platform already keeps
(``ai_calls`` for provider attempts, ``audit_log`` for who asked). Read-only aggregation; no prompt or answer text is stored or shown."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict

from sqlalchemy import func

from db import AICall, AuditLog

AI_AUDIT_ACTIONS = ("knowledge.ask", "conversation.ask", "agent.plan", "agent.approve", "agent.reject", "agent.step", "agent.cancel",
                    "investigation.create")
MAX_ROWS = 50000


def _pctl(values, q):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def usage(db, workspace_id: str, days: int = 30) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.query(AICall).filter(AICall.workspace_id == workspace_id, AICall.created_at >= since).order_by(AICall.created_at.desc()).limit(MAX_ROWS).all()
    by_task: Dict[str, dict] = defaultdict(lambda: {"calls": 0, "failures": 0, "input_tokens": 0, "output_tokens": 0, "cost_cents": 0.0, "lat": [], "estimated": 0})
    by_provider: Dict[str, dict] = defaultdict(lambda: {"calls": 0, "failures": 0, "cost_cents": 0.0})
    by_day: Dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost_cents": 0.0})
    errors: Dict[str, int] = defaultdict(int)
    traces = set()
    for r in rows:
        if r.outcome == "skipped":
            continue
        t, p = by_task[r.task_type], by_provider[r.provider]
        t["calls"] += 1; p["calls"] += 1                                    # noqa: E702
        if r.outcome == "failure":
            t["failures"] += 1; p["failures"] += 1                          # noqa: E702
            errors[r.error_category or "unknown"] += 1
        t["input_tokens"] += r.input_tokens or 0
        t["output_tokens"] += r.output_tokens or 0
        t["cost_cents"] += r.estimated_cost_cents or 0.0
        p["cost_cents"] += r.estimated_cost_cents or 0.0
        t["estimated"] += 1 if r.usage_estimated else 0
        if r.latency_ms is not None:
            t["lat"].append(r.latency_ms)
        d = by_day[r.created_at.date().isoformat()]
        d["calls"] += 1; d["cost_cents"] += r.estimated_cost_cents or 0.0   # noqa: E702
        traces.add(r.trace_id)
    tasks = {k: {"calls": v["calls"], "failures": v["failures"], "failure_rate": round(v["failures"] / v["calls"], 3),
                 "input_tokens": v["input_tokens"], "output_tokens": v["output_tokens"], "cost_cents": round(v["cost_cents"], 4),
                 "usage_estimated_calls": v["estimated"], "latency_ms_p50": _pctl(v["lat"], 0.5), "latency_ms_p95": _pctl(v["lat"], 0.95)}
             for k, v in sorted(by_task.items())}
    actions = dict(db.query(AuditLog.action, func.count(AuditLog.id)).filter(
        AuditLog.workspace_id == workspace_id, AuditLog.action.in_(AI_AUDIT_ACTIONS), AuditLog.created_at >= since).group_by(AuditLog.action).all())
    return {"window_days": days, "truncated": len(rows) >= MAX_ROWS, "operations": len(traces),
            "total_cost_cents": round(sum(v["cost_cents"] for v in by_task.values()), 4), "by_task": tasks,
            "by_provider": {k: {**v, "cost_cents": round(v["cost_cents"], 4)} for k, v in sorted(by_provider.items())},
            "by_day": {k: {**v, "cost_cents": round(v["cost_cents"], 4)} for k, v in sorted(by_day.items())},
            "top_errors": dict(sorted(errors.items(), key=lambda kv: -kv[1])[:5]), "ai_actions": {a: int(actions.get(a, 0)) for a in AI_AUDIT_ACTIONS},
            "note": "Costs are estimates from token counts; calls whose provider did not report usage are counted in usage_estimated_calls."}
