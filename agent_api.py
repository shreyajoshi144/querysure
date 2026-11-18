"""Controlled-agent routes (Sprint 9). See ``agent.py`` for the safety design.

Roles: analyst+ starts a run; the creator or a workspace admin+ can read it (anyone else gets 404); approving or rejecting a write step
needs the role the tool requires (sync_dataset: analyst, delete_document: admin). The model call is the shared one
(``knowledge_api.generate_answer``: budget, key rules, provider chain, trace).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as OrmSession

import agent
import audit
import config
import knowledge_api
import workspaces as ws_service
from auth import get_current_user
from db import AgentRun, AgentStep, User, get_db

_ok, _err = knowledge_api._ok, knowledge_api._err


class RunIn(BaseModel):
    goal: str = Field(..., min_length=1, max_length=4000)
    api_key: Optional[str] = Field(default=None, max_length=300)


def build_router(limiter, *, ai) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["agent"])

    def _wserr(exc):
        return _err(exc.message, exc.status_code, exc.code)

    def _load(db, user, run_id):
        """(run, workspace, None) or (None, None, error). 404 when missing, in a workspace you are not in, or not yours and you are not admin."""
        run = db.get(AgentRun, run_id) if 0 < len(run_id or "") <= 64 else None
        nope = _err("Run not found.", 404, "RUN_NOT_FOUND")
        if run is None:
            return None, None, nope
        try:
            ws, member = ws_service.require_role(db, user, run.workspace_id, "viewer")
        except ws_service.WorkspaceError:
            return None, None, nope
        if run.user_id != user.id and ws_service.ROLE_RANK[member.role] < ws_service.ROLE_RANK["admin"]:
            return None, None, nope
        return run, ws, None

    @router.get("/agent/tools")
    def tools(current_user: User = Depends(get_current_user)):
        return _ok({"tools": agent.catalog(), "max_steps": config.AGENT_MAX_STEPS, "enabled": config.AGENT_ENABLED})

    @router.get("/workspaces/{workspace_id}/agent/runs")
    def list_runs(workspace_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws, member = ws_service.require_role(db, current_user, workspace_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        q = db.query(AgentRun).filter(AgentRun.workspace_id == ws.id)
        if ws_service.ROLE_RANK[member.role] < ws_service.ROLE_RANK["admin"]:
            q = q.filter(AgentRun.user_id == current_user.id)
        return _ok({"runs": [agent.run_to_dict(r) for r in q.order_by(AgentRun.created_at.desc()).limit(50).all()]})

    @router.post("/workspaces/{workspace_id}/agent/runs")
    @limiter.limit("10/minute")
    def start_run(request: Request, workspace_id: str, payload: RunIn, background_tasks: BackgroundTasks,
                  current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws, _ = ws_service.require_role(db, current_user, workspace_id, "analyst")
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        goal = payload.goal.strip()
        if not goal:
            return _err("A goal is required.", 400, "INVALID_GOAL")
        if len(goal) > config.AGENT_GOAL_MAX_CHARS:
            return _err(f"The goal is too long (max {config.AGENT_GOAL_MAX_CHARS} characters).", 413, "GOAL_TOO_LONG")
        if not config.AGENT_ENABLED or config.AI_KILL_SWITCH:
            return _err("The agent is temporarily disabled.", 503, "AGENT_DISABLED", {"Retry-After": "300"})
        api_key = ai.resolve_key(payload.api_key)
        if not ai.configured(api_key):
            return _err("AI is not configured on this server." if ai.is_production else
                        "No LLM provider configured. Add your own API key in Settings, or set GROQ_API_KEY in .env.", 503, "NO_LLM_PROVIDER")
        ctx = agent.Ctx(db, current_user, ws, background_tasks)
        datasets = [{"id": d["id"], "name": d["name"], "status": d["status"]} for d in agent._list_datasets(ctx, {})["datasets"]]
        documents = agent._list_documents(ctx, {})["documents"]
        done, failure = knowledge_api.generate_answer(
            db, ai, request, current_user, ws, goal, [], api_key, system=agent.system_prompt(),
            prompt=agent.build_prompt(goal, datasets, documents), parse=lambda text: agent.parse_plan(text, ctx),
            retry_note="Your previous reply was rejected. Reply with ONE valid JSON plan using only the registered tools, "
                       "at most %d steps, ids taken from the workspace block." % config.AGENT_MAX_STEPS,
            task="agent_plan", error_code="AGENT_PLAN_FAILED")
        if failure is not None:
            return failure
        run = agent.create_run(db, current_user, ws, goal, done["parsed"], done["provider"], done["trace_id"])
        db.commit()
        audit.record(ws.id, "agent.plan", actor_id=current_user.id, target_type="agent_run", target_id=run.id,
                     details={"steps": len(done["parsed"]["steps"])})
        agent.advance(ctx, run)
        return _ok({"run": agent.run_to_dict(run, agent.steps_of(db, run.id)), "usage": done["usage"]}, 201)

    @router.get("/agent/runs/{run_id}")
    def get_run(run_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        run, _, failure = _load(db, current_user, run_id)
        if failure is not None:
            return failure
        return _ok({"run": agent.run_to_dict(run, agent.steps_of(db, run.id))})

    def _decide(run_id, step_id, approve, background_tasks, user, db):
        run, ws, failure = _load(db, user, run_id)
        if failure is not None:
            return failure
        step = db.get(AgentStep, step_id)
        if step is None or step.run_id != run.id:
            return _err("Step not found.", 404, "STEP_NOT_FOUND")
        try:
            agent.decide(agent.Ctx(db, user, ws, background_tasks), run, step, approve)
        except agent.AgentError as exc:
            return _err(exc.message, 503 if exc.code == "AGENT_DISABLED" else 409, exc.code)
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        return _ok({"run": agent.run_to_dict(run, agent.steps_of(db, run.id))})

    @router.post("/agent/runs/{run_id}/steps/{step_id}/approve")
    @limiter.limit("30/minute")
    def approve(request: Request, run_id: str, step_id: int, background_tasks: BackgroundTasks,
                current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        return _decide(run_id, step_id, True, background_tasks, current_user, db)

    @router.post("/agent/runs/{run_id}/steps/{step_id}/reject")
    @limiter.limit("30/minute")
    def reject(request: Request, run_id: str, step_id: int, background_tasks: BackgroundTasks,
               current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        return _decide(run_id, step_id, False, background_tasks, current_user, db)

    @router.post("/agent/runs/{run_id}/cancel")
    def cancel(run_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        run, ws, failure = _load(db, current_user, run_id)
        if failure is not None:
            return failure
        if run.status not in ("awaiting_approval", "planned"):
            return _err("Only a run that is still waiting can be cancelled.", 409, "RUN_NOT_CANCELLABLE")
        agent.cancel(agent.Ctx(db, current_user, ws), run)
        audit.record(ws.id, "agent.cancel", actor_id=current_user.id, target_type="agent_run", target_id=run.id)
        return _ok({"run": agent.run_to_dict(run, agent.steps_of(db, run.id))})

    return router
