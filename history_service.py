"""Per-user query history: persistence + lookup. Every function takes the OWNER's user id and filters on it, so a row
that belongs to another user is indistinguishable from a row that does not exist (no cross-tenant read, no oracle)."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from db import QueryHistory

logger = logging.getLogger("querysure.history")

MAX_HISTORY_PER_USER = 500
MAX_STORED_ROWS = 200
MAX_RESULT_JSON_BYTES = 200_000
MAX_SQL_CHARS = 20_000
MAX_QUESTION_CHARS = 4_000


def _bounded_result(exec_result: Optional[dict]) -> Optional[dict]:
    if not exec_result:
        return None
    cols = list(exec_result.get("columns") or [])
    rows = list(exec_result.get("rows") or [])
    stored = rows[:MAX_STORED_ROWS]
    out = {"columns": cols, "rows": stored, "total_rows": len(rows),
           "truncated": len(stored) < len(rows) or bool((exec_result.get("execution_metadata") or {}).get("truncated"))}
    while stored and len(json.dumps(out, default=str)) > MAX_RESULT_JSON_BYTES:
        stored = stored[: max(1, len(stored) // 2)]
        out["rows"], out["truncated"] = stored, True
        if len(stored) == 1:
            break
    return out


def dataset_ref_from_schema(schema: Optional[dict]) -> Dict[str, Any]:
    schema = schema or {}
    tables = list((schema.get("tables") or {}).keys())
    fp = str(schema.get("dataset_fingerprint") or schema.get("dataset_id") or "")
    return {"dataset_id": fp[:16] or None, "tables": tables[:8], "table_count": len(tables),
            "label": (tables[0] if len(tables) == 1 else f"{len(tables)} tables") if tables else "Unknown dataset"}


def record(db: Session, user, session_row, sql: str, status: str, exec_result: Optional[dict], error: Optional[str] = None,
           question: Optional[str] = None) -> Optional[str]:
    """Insert one history row. Best effort: history must never break query execution."""
    try:
        orig = ((session_row.sql_result or {}).get("sql") or "").strip() if isinstance(session_row.sql_result, dict) else ""
        verification = (exec_result or {}).get("verification") or {}
        row = QueryHistory(
            user_id=user.id, session_id=session_row.id,
            question=((question or session_row.question or None) and (question or session_row.question)[:MAX_QUESTION_CHARS]),
            sql=sql[:MAX_SQL_CHARS], sql_edited=bool(orig and orig != sql.strip()),
            dataset_ref=dataset_ref_from_schema(session_row.schema), status=status[:32],
            row_count=(exec_result or {}).get("row_count"), error=(error or None) and str(error)[:500],
            result=_bounded_result(exec_result),
            answer_meta={"verification_status": verification.get("status"), "verified": bool(verification.get("verified")),
                         "correctness": (exec_result or {}).get("correctness")},
        )
        db.add(row)
        db.flush()
        hid = row.id
        # retention: keep the newest MAX_HISTORY_PER_USER per user
        old = (db.query(QueryHistory.id).filter(QueryHistory.user_id == user.id)
               .order_by(QueryHistory.created_at.desc()).offset(MAX_HISTORY_PER_USER).all())
        if old:
            db.query(QueryHistory).filter(QueryHistory.id.in_([o[0] for o in old])).delete(synchronize_session=False)
        db.commit()
        return hid
    except Exception:  # noqa: BLE001
        logger.exception("could not record query history")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def attach_reliability(db: Session, user_id: str, history_id: Optional[str], reliability: dict) -> None:
    if not history_id:
        return
    try:
        row = db.query(QueryHistory).filter(QueryHistory.id == history_id, QueryHistory.user_id == user_id).first()
        if row is None:
            return
        meta = dict(row.answer_meta or {})
        meta["reliability_level"] = reliability.get("reliability_level")
        meta["reliability_score"] = reliability.get("reliability_score")
        row.answer_meta = meta
        db.commit()
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _summary(r: QueryHistory) -> Dict[str, Any]:
    return {"id": r.id, "question": r.question, "sql": r.sql, "status": r.status, "row_count": r.row_count,
            "created_at": r.created_at.isoformat() if r.created_at else None, "dataset": r.dataset_ref,
            "sql_edited": bool(r.sql_edited), "has_result": bool(r.result),
            "answer_meta": r.answer_meta}


def list_history(db: Session, user_id: str, q: str = "", limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    query = db.query(QueryHistory).filter(QueryHistory.user_id == user_id)
    q = (q or "").strip()[:200]
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter(or_(QueryHistory.question.ilike(like, escape="\\"), QueryHistory.sql.ilike(like, escape="\\")))
    total = query.count()
    rows = query.order_by(QueryHistory.created_at.desc(), QueryHistory.id).offset(offset).limit(limit).all()
    return {"items": [_summary(r) for r in rows], "total": total, "limit": limit, "offset": offset}


def get_entry(db: Session, user_id: str, entry_id: str) -> Optional[Dict[str, Any]]:
    r = db.query(QueryHistory).filter(QueryHistory.id == entry_id, QueryHistory.user_id == user_id).first()
    if r is None:
        return None
    return {**_summary(r), "result": r.result, "error": r.error}


def delete_entry(db: Session, user_id: str, entry_id: str) -> bool:
    n = db.query(QueryHistory).filter(QueryHistory.id == entry_id, QueryHistory.user_id == user_id).delete(synchronize_session=False)
    db.commit()
    return bool(n)


def clear_all(db: Session, user_id: str) -> int:
    n = db.query(QueryHistory).filter(QueryHistory.user_id == user_id).delete(synchronize_session=False)
    db.commit()
    return n
