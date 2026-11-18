"""Assistant conversations (Sprint 8): persisted, per-user chats inside a workspace that answer from the knowledge hub.

Reuses Sprint 7 end to end -- ``knowledge.search`` retrieval, ``knowledge.build_prompt`` / ``parse_answer`` and the shared model
call -- and adds only what a chat needs: stored turns, follow-up context, and a deterministic intent check. No model call is made
to classify intent. Questions about table data are NOT answered here (that is Text-to-SQL on a dataset); the assistant says so.
"""
from __future__ import annotations

import re
from typing import List, Optional

import config
import knowledge
from db import Conversation, ConversationMessage

_DATA_HINT = re.compile(
    r"\b(how many|number of|count of|total|sum of|average|avg|median|per (?:day|week|month|quarter|year)|top \d+|highest|lowest|"
    r"trend|group(?:ed)? by|rows?|columns?|revenue by|sales by)\b", re.I)
DATA_NOTE = ("This looks like a question about table data. The assistant answers from your uploaded documents only; "
             "ask data questions from the Ask-data panel on a dataset, where they are answered with SQL.")


def classify(question: str) -> str:
    """'data_question' when the wording asks for a figure computed from rows, else 'knowledge'. Used only when no document passage matched."""
    return "data_question" if _DATA_HINT.search(question or "") else "knowledge"


def title_from(question: str) -> str:
    t = re.sub(r"\s+", " ", knowledge.clean(question)).strip()
    return (t[:60].rstrip() + "…") if len(t) > 60 else (t or "New chat")


def history_for_prompt(messages: List[ConversationMessage]) -> List[dict]:
    """The last few turns, oldest first, as {role, content}. Withheld/failed turns are plain text and carry no extra authority."""
    turns = messages[-config.CONVERSATION_HISTORY_TURNS:] if config.CONVERSATION_HISTORY_TURNS else []
    return [{"role": m.role, "content": m.content} for m in turns]


_FOLLOWUP = re.compile(r"^(and|also|then|what about|how about|but)\b|\b(it|its|they|them|their|that|those|this|these|he|she)\b", re.I)


def find_passages(db, workspace_id: str, question: str, history: List[dict]) -> List[dict]:
    """Retrieve for the question. Only a follow-up (short, or leaning on 'it/that/and ...') is searched together with the previous user
    question; a self-contained question never borrows the earlier topic, so an off-topic or data question still finds nothing."""
    prior = next((m["content"] for m in reversed(history) if m["role"] == "user"), None)
    if prior and (len(re.findall(r"\w+", question)) <= 6 or _FOLLOWUP.search(question)):
        return knowledge.search(db, workspace_id, f"{prior} {question}")
    return knowledge.search(db, workspace_id, question)


def get_owned(db, user_id: str, conversation_id: str) -> Optional[Conversation]:
    return db.query(Conversation).filter(Conversation.id == conversation_id, Conversation.user_id == user_id).first()


def messages_of(db, conversation_id: str) -> List[ConversationMessage]:
    return db.query(ConversationMessage).filter(ConversationMessage.conversation_id == conversation_id).order_by(ConversationMessage.id).all()


def delete_conversations(db, conversation_ids: List[str]) -> int:
    """Remove chats and their messages (no commit). Messages are deleted explicitly: SQLite does not enforce the cascade."""
    if not conversation_ids:
        return 0
    db.query(ConversationMessage).filter(ConversationMessage.conversation_id.in_(conversation_ids)).delete(synchronize_session=False)
    return db.query(Conversation).filter(Conversation.id.in_(conversation_ids)).delete(synchronize_session=False)


def message_to_dict(m: ConversationMessage) -> dict:
    return {"id": m.id, "role": m.role, "content": m.content, "intent": m.intent, "grounded": m.grounded, "citations": m.citations or [],
            "sources": m.sources or [], "provider_used": m.provider, "trace_id": m.trace_id,
            "created_at": m.created_at.isoformat() if m.created_at else None}


def conversation_to_dict(c: Conversation, message_count: Optional[int] = None) -> dict:
    d = {"id": c.id, "workspace_id": c.workspace_id, "title": c.title,
         "created_at": c.created_at.isoformat() if c.created_at else None, "updated_at": c.updated_at.isoformat() if c.updated_at else None}
    if message_count is not None:
        d["message_count"] = message_count
    return d
