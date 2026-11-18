"""Assistant conversation routes (Sprint 8). A chat is private to its creator: any other user -- even a workspace admin -- gets 404.
Membership is re-checked on every call (viewer+), so a removed member loses access. The model call is ``knowledge_api.generate_answer``
(existing budget, key rules, provider chain, trace); retrieved text stays untrusted. See ``assistant.py``.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session as OrmSession

import assistant
import audit
import config
import knowledge_api
import workspaces as ws_service
from auth import get_current_user
from db import Conversation, ConversationMessage, User, _now, get_db

_ok, _err = knowledge_api._ok, knowledge_api._err


class CreateIn(BaseModel):
    title: Optional[str] = Field(default=None, max_length=120)


class RenameIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)


class MessageIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    api_key: Optional[str] = Field(default=None, max_length=300)


def build_router(limiter, *, ai) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["assistant"])

    def _wserr(exc: ws_service.WorkspaceError) -> JSONResponse:
        return _err(exc.message, exc.status_code, exc.code)

    def _load(db, user, conversation_id):
        """(conversation, workspace) or (None, error response). 404 for a stranger's chat or a lost membership."""
        conv = assistant.get_owned(db, user.id, conversation_id)
        if conv is None:
            return None, None, _err("Conversation not found.", 404, "CONVERSATION_NOT_FOUND")
        try:
            ws, _ = ws_service.require_role(db, user, conv.workspace_id, "viewer")
        except ws_service.WorkspaceError:
            return None, None, _err("Conversation not found.", 404, "CONVERSATION_NOT_FOUND")
        return conv, ws, None

    @router.get("/workspaces/{workspace_id}/conversations")
    def list_conversations(workspace_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws, _ = ws_service.require_role(db, current_user, workspace_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        rows = db.query(Conversation).filter(Conversation.workspace_id == ws.id, Conversation.user_id == current_user.id) \
            .order_by(Conversation.updated_at.desc()).limit(config.CONVERSATIONS_MAX_PER_USER_WORKSPACE).all()
        counts = dict(db.query(ConversationMessage.conversation_id, func.count(ConversationMessage.id))
                      .filter(ConversationMessage.conversation_id.in_([c.id for c in rows] or [""])).group_by(ConversationMessage.conversation_id).all())
        return _ok({"conversations": [assistant.conversation_to_dict(c, counts.get(c.id, 0)) for c in rows]})

    @router.post("/workspaces/{workspace_id}/conversations")
    @limiter.limit("30/minute")
    def create_conversation(request: Request, workspace_id: str, payload: CreateIn, current_user: User = Depends(get_current_user),
                            db: OrmSession = Depends(get_db)):
        try:
            ws, _ = ws_service.require_role(db, current_user, workspace_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        owned = db.query(func.count(Conversation.id)).filter(Conversation.workspace_id == ws.id, Conversation.user_id == current_user.id).scalar() or 0
        if owned >= config.CONVERSATIONS_MAX_PER_USER_WORKSPACE:
            return _err(f"You have reached the limit of {config.CONVERSATIONS_MAX_PER_USER_WORKSPACE} conversations in this workspace. Delete one first.",
                        409, "CONVERSATION_LIMIT")
        conv = Conversation(workspace_id=ws.id, user_id=current_user.id, title=assistant.title_from(payload.title or ""))
        db.add(conv)
        db.commit()
        audit.record(ws.id, "conversation.create", actor_id=current_user.id, target_type="conversation", target_id=conv.id)
        return _ok({"conversation": assistant.conversation_to_dict(conv, 0)}, 201)

    @router.get("/conversations/{conversation_id}")
    def get_conversation(conversation_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        conv, _, failure = _load(db, current_user, conversation_id)
        if failure is not None:
            return failure
        msgs = assistant.messages_of(db, conv.id)
        return _ok({"conversation": assistant.conversation_to_dict(conv, len(msgs)), "messages": [assistant.message_to_dict(m) for m in msgs]})

    @router.patch("/conversations/{conversation_id}")
    def rename_conversation(conversation_id: str, payload: RenameIn, current_user: User = Depends(get_current_user),
                            db: OrmSession = Depends(get_db)):
        conv, _, failure = _load(db, current_user, conversation_id)
        if failure is not None:
            return failure
        conv.title = assistant.title_from(payload.title)
        db.commit()
        return _ok({"conversation": assistant.conversation_to_dict(conv)})

    @router.delete("/conversations/{conversation_id}")
    def delete_conversation(conversation_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        conv, ws, failure = _load(db, current_user, conversation_id)
        if failure is not None:
            return failure
        assistant.delete_conversations(db, [conv.id])
        db.commit()
        audit.record(ws.id, "conversation.delete", actor_id=current_user.id, target_type="conversation", target_id=conversation_id)
        return _ok({"deleted": True})

    @router.post("/conversations/{conversation_id}/messages")
    @limiter.limit("20/minute")
    def send_message(request: Request, conversation_id: str, payload: MessageIn, current_user: User = Depends(get_current_user),
                     db: OrmSession = Depends(get_db)):
        conv, ws, failure = _load(db, current_user, conversation_id)
        if failure is not None:
            return failure
        question = payload.question.strip()
        if not question:
            return _err("A question is required.", 400, "INVALID_QUESTION")
        if len(question) > ai.max_question_length:
            return _err(f"Question is too long (max {ai.max_question_length} characters).", 413, "QUESTION_TOO_LONG")
        prior = assistant.messages_of(db, conv.id)
        if len(prior) + 2 > config.CONVERSATION_MAX_MESSAGES:
            return _err("This conversation is full. Start a new one.", 409, "CONVERSATION_FULL")
        history = assistant.history_for_prompt(prior)
        passages = assistant.find_passages(db, ws.id, question, history)
        result = {"intent": "knowledge", "content": "", "grounded": False, "citations": [], "sources": [], "provider": None, "trace_id": None}
        usage = None
        if not passages:    # nothing relevant: no model call, no spend, and say why
            result["intent"] = assistant.classify(question)
            result["content"] = assistant.DATA_NOTE if result["intent"] == "data_question" else \
                "No passage in this workspace's documents is relevant to that question."
        else:
            if config.AI_KILL_SWITCH:
                return _err("AI features are temporarily disabled. Please try again later.", 503, "AI_DISABLED", {"Retry-After": "300"})
            api_key = ai.resolve_key(payload.api_key)
            if not ai.configured(api_key):
                return _err("AI answers are not configured on this server." if ai.is_production else
                            "No LLM provider configured. Add your own API key in Settings, or set GROQ_API_KEY in .env.", 503, "NO_LLM_PROVIDER")
            done, failure = knowledge_api.generate_answer(db, ai, request, current_user, ws, question, passages, api_key, history)
            if failure is not None:
                return failure
            parsed, usage = done["parsed"], done["usage"]
            result.update(content=parsed["answer"] or parsed.get("note") or "The model's answer could not be shown.", grounded=parsed["grounded"],
                          citations=parsed["citations"], sources=done["sources"], provider=done["provider"], trace_id=done["trace_id"])
        user_msg = ConversationMessage(conversation_id=conv.id, role="user", content=question)
        bot_msg = ConversationMessage(conversation_id=conv.id, role="assistant", content=result["content"], intent=result["intent"],
                                      grounded=result["grounded"], citations=result["citations"], sources=result["sources"],
                                      provider=result["provider"], trace_id=result["trace_id"])
        db.add_all([user_msg, bot_msg])
        if not prior:
            conv.title = assistant.title_from(question)
        conv.updated_at = _now()
        db.commit()
        db.refresh(conv)
        audit.record(ws.id, "conversation.ask", actor_id=current_user.id, target_type="conversation", target_id=conv.id,
                     details={"intent": result["intent"], "count": len(passages), "cited": len(result["citations"]), "grounded": result["grounded"]})
        return _ok({"conversation": assistant.conversation_to_dict(conv, len(prior) + 2), "user_message": assistant.message_to_dict(user_msg),
                    "message": assistant.message_to_dict(bot_msg), **({"usage": usage} if usage else {})})

    return router
