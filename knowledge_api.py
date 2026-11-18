"""Knowledge hub routes (Sprint 7): upload / list / delete documents, search passages, ask a question and get a cited answer.

Every route authenticates with ``get_current_user`` and authorizes through ``workspaces.require_role``: viewer+ list, search and ask;
analyst+ upload; admin (or the uploader, analyst+) delete. A non-member gets 404 for the workspace and for every document id.
Retrieved text is untrusted: see ``knowledge.SYSTEM_PROMPT``. AI budget, key handling and the provider chain are the existing ones,
passed in as ``ai`` so there is no second provider system.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as OrmSession

import audit
import config
import knowledge
import workspaces as ws_service
from ai import trace as ai_trace
from ai.types import ProviderError
from ai.usage import aggregate_usage
from auth import get_current_user
from db import KnowledgeDocument, User, get_db
from modules.ingestion import sanitize_filename

logger = logging.getLogger("querysure.knowledge")


class AskIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    document_ids: Optional[List[str]] = Field(default=None, max_length=50)
    api_key: Optional[str] = Field(default=None, max_length=300)


def _ok(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content={"success": True, **data})


def _err(message: str, code: int, error_code: str, headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse(status_code=code, content={"success": False, "error": message, "error_code": error_code}, headers=headers)


def generate_answer(db, ai, request, user, ws, question, passages, api_key, history=None, *, system=None, prompt=None, parse=None,
                    retry_note=None, task=None, error_code="KNOWLEDGE_ANSWER_FAILED"):
    """One cited-answer model call with the existing budget, provider chain and trace (shared by the knowledge and conversation
    routes). Returns ``(result, None)`` or ``(None, error_response)``; ``result`` has parsed/sources/provider/trace_id/usage."""
    prompt = prompt if prompt is not None else knowledge.build_prompt(question, passages, history)
    system = system or knowledge.SYSTEM_PROMPT
    parse = parse or (lambda text: knowledge.parse_answer(text, passages))
    retry_note = retry_note or 'Your previous reply was not valid JSON. Reply with ONE JSON object with the keys "answer", "citations", "insufficient".'
    refusal, hold = ai.reserve(db, user, question, prompt)
    if refusal is not None:
        return None, refusal
    attempts, parsed, provider = [], None, None
    try:
        with ai_trace.start(user_id=user.id, workspace_id=ws.id, request_id=getattr(request.state, "request_id", None)) as tr:
            for round_no in range(2):     # one retry when the reply is not the required JSON
                text, provider, got = ai.run(system, prompt if round_no == 0 else prompt + "\n\n" + retry_note, api_key,
                                             **({"task": task} if task else {}))
                attempts.extend(got)
                try:
                    parsed = parse(text)
                    break
                except ValueError:
                    continue
    except ProviderError as exc:
        attempts.extend(getattr(exc, "attempts", []) or [])
        ai.settle(db, user.id, hold, float(aggregate_usage(attempts).get("cost_cents") or 0.0), False)
        logger.warning("knowledge answer failed: %s", type(exc).__name__)
        return None, _err("The AI request failed: every configured AI provider was unavailable or returned an error. Please try again.", 502,
                          error_code)
    except BaseException:
        ai.settle(db, user.id, hold, 0.0, False)
        raise
    usage = aggregate_usage(attempts)
    ai.settle(db, user.id, hold, float(usage.get("cost_cents") or 0.0) or 0.0001, parsed is not None)
    if parsed is None:
        return None, _err("The model did not return a usable reply. Please try again.", 502, error_code)
    cited = set(parsed.get("citations") or [])
    sources = [{"n": i, "cited": i in cited, "chunk_id": p["chunk_id"], "document_id": p["document_id"], "document_name": p["document_name"],
                "page": p["page"], "heading": p["heading"], "text": p["text"][:1200], "score": p["score"]} for i, p in enumerate(passages, 1)]
    return {"parsed": parsed, "sources": sources, "provider": provider, "trace_id": tr.trace_id,
            "usage": {k: usage[k] for k in ("input_tokens", "output_tokens", "cost_cents", "estimated")}}, None


def build_router(limiter, *, ai, stream_to_disk, multipart_overhead: int, upload_dir) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["knowledge"])

    def _wserr(exc: ws_service.WorkspaceError) -> JSONResponse:
        return _err(exc.message, exc.status_code, exc.code)

    @router.get("/workspaces/{workspace_id}/knowledge")
    def list_documents(workspace_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws, member = ws_service.require_role(db, current_user, workspace_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        rows = db.query(KnowledgeDocument).filter(KnowledgeDocument.workspace_id == ws.id).order_by(KnowledgeDocument.created_at.desc()).all()
        emails = ws_service._emails(db, [r.created_by for r in rows])
        rank = ws_service.ROLE_RANK
        return _ok({"documents": [knowledge.document_to_dict(r, emails, rank[member.role] >= rank["admin"] or
                                                              (r.created_by == current_user.id and rank[member.role] >= rank["analyst"]))
                                  for r in rows],
                    "your_role": member.role, "limits": {"max_file_mb": config.KNOWLEDGE_MAX_FILE_MB,
                                                         "max_documents": config.KNOWLEDGE_MAX_DOCS_PER_WORKSPACE,
                                                         "formats": sorted(knowledge.EXTENSIONS)}})

    @router.post("/workspaces/{workspace_id}/knowledge")
    @limiter.limit("20/minute")
    def upload_document(request: Request, workspace_id: str, file: UploadFile = File(...),
                        current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws, _ = ws_service.require_role(db, current_user, workspace_id, "analyst")
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        fname = sanitize_filename(file.filename)
        ext = os.path.splitext(fname)[1].lower()
        if ext not in knowledge.EXTENSIONS:
            return _err("Unsupported file type. Upload a .txt, .md, .pdf or .docx file.", 400, "UNSUPPORTED_FILE_TYPE")
        limit = config.KNOWLEDGE_MAX_FILE_MB * 1024 * 1024
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit + multipart_overhead:
            return _err(f"File too large. Maximum allowed size is {config.KNOWLEDGE_MAX_FILE_MB} MB.", 413, "FILE_TOO_LARGE")
        path = os.path.join(upload_dir(), f"kb-{uuid.uuid4()}{ext}")
        try:
            if not stream_to_disk(file, path, limit):
                return _err(f"File too large. Maximum allowed size is {config.KNOWLEDGE_MAX_FILE_MB} MB.", 413, "FILE_TOO_LARGE")
            try:
                doc = knowledge.index_document(db, workspace_id=ws.id, user_id=current_user.id, name=fname, ext=ext, path=path,
                                               size=os.path.getsize(path))
            except knowledge.KnowledgeError as exc:
                db.rollback()
                return _err(exc.message, exc.status, exc.code)
            except Exception:  # noqa: BLE001 - a racing identical upload hits the unique index
                db.rollback()
                logger.exception("knowledge indexing failed")
                return _err("The document could not be indexed.", 500, "INDEXING_FAILED")
        finally:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        audit.record(ws.id, "knowledge.upload", actor_id=current_user.id, target_type="document", target_id=doc.id,
                     details={"name": doc.name, "format": doc.file_format, "bytes": doc.bytes, "count": doc.chunk_count})
        return _ok({"document": knowledge.document_to_dict(doc, {doc.created_by: current_user.email}, True)}, 201)

    @router.delete("/knowledge/{document_id}")
    def delete_document(document_id: str, current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        doc = db.get(KnowledgeDocument, document_id) if 0 < len(document_id or "") <= 64 else None
        if doc is None:
            return _err("Document not found.", 404, "DOCUMENT_NOT_FOUND")
        try:
            ws, member = ws_service.require_role(db, current_user, doc.workspace_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _err("Document not found.", 404, "DOCUMENT_NOT_FOUND") if exc.status_code == 404 else _wserr(exc)
        rank = ws_service.ROLE_RANK
        if not (rank[member.role] >= rank["admin"] or (doc.created_by == current_user.id and rank[member.role] >= rank["analyst"])):
            return _err("Only an admin or the uploader can delete this document.", 403, "FORBIDDEN")
        name, chunks = doc.name, doc.chunk_count
        knowledge.delete_documents(db, [doc.id])
        db.commit()
        audit.record(ws.id, "knowledge.delete", actor_id=current_user.id, target_type="document", target_id=document_id,
                     details={"name": name, "count": chunks})
        return _ok({"deleted": document_id})

    @router.get("/workspaces/{workspace_id}/knowledge/search")
    @limiter.limit("60/minute")
    def search_passages(request: Request, workspace_id: str, q: str = "", limit: int = 6,
                        current_user: User = Depends(get_current_user), db: OrmSession = Depends(get_db)):
        try:
            ws, _ = ws_service.require_role(db, current_user, workspace_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        q = (q or "").strip()
        if not q or len(q) > 1000:
            return _err("Enter a search between 1 and 1000 characters.", 400, "INVALID_QUERY")
        hits = knowledge.search(db, ws.id, q, top_k=max(1, min(int(limit or 6), 20)))
        return _ok({"results": hits, "query": q})

    @router.post("/workspaces/{workspace_id}/knowledge/ask")
    @limiter.limit("20/minute")
    def ask(request: Request, workspace_id: str, payload: AskIn, current_user: User = Depends(get_current_user),
            db: OrmSession = Depends(get_db)):
        try:
            ws, _ = ws_service.require_role(db, current_user, workspace_id, "viewer")
        except ws_service.WorkspaceError as exc:
            return _wserr(exc)
        question = payload.question.strip()
        if not question:
            return _err("A question is required.", 400, "INVALID_QUESTION")
        if len(question) > ai.max_question_length:
            return _err(f"Question is too long (max {ai.max_question_length} characters).", 413, "QUESTION_TOO_LONG")
        if config.AI_KILL_SWITCH:
            return _err("AI features are temporarily disabled. Please try again later.", 503, "AI_DISABLED", {"Retry-After": "300"})
        passages = knowledge.search(db, ws.id, question, document_ids=payload.document_ids)
        base = {"question_chars": len(question), "retrieved": len(passages)}
        if not passages:    # nothing relevant: say so, spend nothing, call no model
            audit.record(ws.id, "knowledge.ask", actor_id=current_user.id, target_type="workspace", target_id=ws.id,
                         details={"count": 0, "grounded": False})
            return _ok({"answer": None, "grounded": False, "insufficient": True, "sources": [], "citations": [], "provider_used": None,
                        "note": "No passage in this workspace's documents is relevant to the question.", **base})
        api_key = ai.resolve_key(payload.api_key)
        if not ai.configured(api_key):
            return _err("AI answers are not configured on this server." if ai.is_production else
                        "No LLM provider configured. Add your own API key in Settings, or set GROQ_API_KEY in .env.", 503, "NO_LLM_PROVIDER")
        done, failure = generate_answer(db, ai, request, current_user, ws, question, passages, api_key)
        if failure is not None:
            return failure
        parsed, cited = done["parsed"], set(done["parsed"]["citations"])
        audit.record(ws.id, "knowledge.ask", actor_id=current_user.id, target_type="workspace", target_id=ws.id,
                     details={"count": len(passages), "cited": len(cited), "grounded": parsed["grounded"]})
        return _ok({"answer": parsed["answer"], "grounded": parsed["grounded"], "insufficient": parsed["insufficient"], "citations": parsed["citations"],
                    "sources": done["sources"], "provider_used": done["provider"], "trace_id": done["trace_id"], "usage": done["usage"],
                    **({"note": parsed["note"]} if parsed.get("note") else {}), **base})

    return router
