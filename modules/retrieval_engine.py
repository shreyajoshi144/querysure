"""
Retrieval Engine
TF-IDF-based retrieval over semantic documents for relevant tables, columns, metrics, and relationships.
No external ML dependencies required.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from collections import Counter, OrderedDict
from typing import Any, Dict, List, Optional

# Per-key index cache (key = session id, or a persist_directory).
#
# SECURITY: this used to be a single shared slot ("__ephemeral__") that every
# user's connect/retrieve call read and overwrote, so one user's retrieval
# could return another user's table/column names. Now:
#   1. Callers that pass no key get NO caching at all (index is built locally
#      and discarded) -- there is no shared default slot any more.
#   2. Every cached entry carries a fingerprint of the exact semantic layer it
#      was built from, and retrieve_context() re-checks it against the
#      semantic layer it was actually handed. A wrong/stale key can therefore
#      only cause a rebuild, never a wrong answer (also covers multi-worker
#      deployments where another worker re-uploaded the session's data).
#   3. Bounded LRU so a long-running process can't grow this without limit.
_CACHE_MAX_ENTRIES = 128
_COLLECTION_CACHE: "OrderedDict[str, Any]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _cache_get(key: str):
    with _CACHE_LOCK:
        entry = _COLLECTION_CACHE.get(key)
        if entry is not None:
            _COLLECTION_CACHE.move_to_end(key)
        return entry


def _cache_put(key: str, entry: dict) -> None:
    with _CACHE_LOCK:
        _COLLECTION_CACHE[key] = entry
        _COLLECTION_CACHE.move_to_end(key)
        while len(_COLLECTION_CACHE) > _CACHE_MAX_ENTRIES:
            _COLLECTION_CACHE.popitem(last=False)


def clear_cache_key(key: str) -> None:
    """Remove one session's retrieval index when that session is flushed."""
    if not key:
        return
    with _CACHE_LOCK:
        _COLLECTION_CACHE.pop(key, None)


def _fingerprint(docs: List[Dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for d in docs:
        h.update(str(d.get("id", "")).encode("utf-8", "replace"))
        h.update(b"\x00")
        h.update(str(d.get("text", "")).encode("utf-8", "replace"))
        h.update(b"\x01")
    return h.hexdigest()


def _build_index(docs: List[Dict[str, Any]]) -> dict:
    vecs, idf = _build_tfidf([d["text"] for d in docs])
    return {"docs": docs, "vecs": vecs, "idf": idf, "fingerprint": _fingerprint(docs)}


# A schema this small is shown to the model in full: leaving out a table the question needs is unrecoverable, while a few
# extra tables cost little prompt. Larger schemas keep the ranked subset plus the tables that connect it.
SMALL_SCHEMA_COLUMNS = 150
MAX_JOIN_PATH_TABLES = 8


def _stem(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        return token[:-1]
    return token


def _tokenize(text: str) -> List[str]:
    # OrderDetail -> order detail, CustomerId -> customer id, "orders" -> order: column and table names written in
    # CamelCase, and plural question words, must meet in the same vocabulary
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(text))
    return [_stem(t) for t in re.findall(r"[a-zA-Z0-9]+", text.lower())]


def _relationship_graph(semantic: dict) -> Dict[str, set]:
    graph: Dict[str, set] = {}
    pairs = semantic.get("relationship_pairs")
    if pairs is None:  # semantic layers saved before relationship_pairs existed: declared foreign keys only
        pairs = [[t, fk.get("to_table")] for t, info in (semantic.get("tables") or {}).items() for fk in info.get("foreign_keys", [])]
    for a, b in pairs:
        if a and b and a != b:
            graph.setdefault(a, set()).add(b)
            graph.setdefault(b, set()).add(a)
    return graph


def _path(graph: Dict[str, set], start: str, goal: str, max_hops: int = 3) -> List[str]:
    frontier, seen = [[start]], {start}
    while frontier:
        path = frontier.pop(0)
        if path[-1] == goal:
            return path
        if len(path) > max_hops:
            continue
        for nxt in sorted(graph.get(path[-1], ())):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(path + [nxt])
    return []


def _build_tfidf(docs: List[str]):
    tokenized = [_tokenize(d) for d in docs]
    df: Counter = Counter()
    for tokens in tokenized:
        for t in set(tokens):
            df[t] += 1
    N = len(docs)
    idf = {t: math.log((N + 1) / (v + 1)) + 1 for t, v in df.items()}

    def vec(tokens):
        tf = Counter(tokens)
        total = max(1, len(tokens))
        return {t: (c / total) * idf.get(t, 1.0) for t, c in tf.items()}

    vecs = [vec(t) for t in tokenized]
    return vecs, idf


def _cosine(v1: dict, v2: dict) -> float:
    common = set(v1) & set(v2)
    if not common:
        return 0.0
    dot = sum(v1[k] * v2[k] for k in common)
    n1 = math.sqrt(sum(x * x for x in v1.values()))
    n2 = math.sqrt(sum(x * x for x in v2.values()))
    return dot / (n1 * n2 + 1e-9)


def _doc_type_priority(doc_type: str) -> int:
    return {"table": 3, "column": 2, "relationship": 2, "metric": 2}.get(doc_type, 1)


def _normalize_docs(semantic: dict) -> List[Dict[str, Any]]:
    docs = []
    for doc in semantic.get("embedding_documents", []):
        item = dict(doc)
        item.setdefault("type", "text")
        item.setdefault("metadata", {})
        item.setdefault("priority", 1)
        docs.append(item)

    for metric_name, definition in semantic.get("metric_definitions", {}).items():
        docs.append({
            "id": f"metric::{metric_name}",
            "type": "metric",
            "table": None,
            "text": f"Metric: {metric_name}\nDefinition: {definition}",
            "metadata": {"metric": metric_name, "definition": definition},
            "priority": 3,
        })

    for rel in semantic.get("relationships_narrative", []):
        docs.append({
            "id": f"relationship::{abs(hash(rel))}",
            "type": "relationship",
            "table": None,
            "text": f"Relationship: {rel}",
            "metadata": {"relationship": rel},
            "priority": 2,
        })
    return docs


def build_vector_store(
    semantic: dict,
    persist_directory: Optional[str] = None,
    cache_key: Optional[str] = None,
) -> dict:
    docs = _normalize_docs(semantic)
    key = cache_key or persist_directory
    if key:
        _cache_put(key, _build_index(docs))
    semantic["vector_store"] = {
        "model_name": "tfidf",
        "doc_count": len(docs),
        "persist_directory": persist_directory,
        "collection_name": "semantic_docs",
    }
    return {
        "success": True,
        "model_name": "tfidf",
        "doc_count": len(docs),
        "persist_directory": persist_directory,
        "collection_name": "semantic_docs",
    }


def retrieve_context(
    question: str,
    semantic: dict,
    top_k_tables: int = 5,
    top_k_columns: int = 12,
    persist_directory: Optional[str] = None,
    cache_key: Optional[str] = None,
) -> dict:
    """Retrieve the most relevant tables, columns, metrics, and relationships for a question."""
    key = cache_key or persist_directory
    fresh_docs = _normalize_docs(semantic)
    cache = _cache_get(key) if key else None
    if cache is None or cache.get("fingerprint") != _fingerprint(fresh_docs):
        cache = _build_index(fresh_docs)
        if key:
            _cache_put(key, cache)
    docs = cache["docs"]
    vecs = cache["vecs"]
    idf = cache["idf"]

    q_tokens = _tokenize(question)
    q_tf = Counter(q_tokens)
    q_total = max(1, len(q_tokens))
    q_vec = {t: (c / q_total) * idf.get(t, 1.0) for t, c in q_tf.items()}

    raw_hits = []
    for i, (doc, vec) in enumerate(zip(docs, vecs)):
        score = _cosine(q_vec, vec)
        boost = _doc_type_priority(doc.get("type", "text")) * 0.01
        boost += min(doc.get("priority", 1), 10) * 0.005
        final = min(1.0, score * 0.8 + boost)
        raw_hits.append({
            "id": doc["id"],
            "text": doc["text"],
            "metadata": doc.get("metadata", {}),
            "score": score,
            "final_score": round(final, 6),
            "type": doc.get("type", "text"),
            "table": doc.get("table"),
            "priority": doc.get("priority", 1),
        })

    raw_hits.sort(key=lambda x: (x["final_score"], x["priority"]), reverse=True)

    table_hits = [h for h in raw_hits if h["type"] == "table"]
    column_hits = [h for h in raw_hits if h["type"] == "column"]
    metric_hits = [h for h in raw_hits if h["type"] == "metric"]
    rel_hits = [h for h in raw_hits if h["type"] == "relationship"]

    selected_tables = []
    selected_columns = []
    table_set: set = set()

    for hit in table_hits:
        if len(selected_tables) >= top_k_tables:
            break
        tname = hit.get("table")
        if not tname or tname in table_set:
            continue
        tinfo = semantic["tables"].get(tname, {})
        selected_tables.append({
            "table": tname,
            "business_name": tinfo.get("business_name", tname),
            "table_type": tinfo.get("table_type", "unknown"),
            "score": hit["final_score"],
            "summary": tinfo.get("table_summary", ""),
            "available_metrics": tinfo.get("available_metrics", []),
            "row_count": tinfo.get("row_count", 0),
        })
        table_set.add(tname)

    for hit in column_hits:
        if len(selected_columns) >= top_k_columns:
            break
        tname = hit.get("table")
        if not tname:
            continue
        md = hit.get("metadata", {})
        selected_columns.append({
            "table": tname,
            "column": md.get("column") or hit["id"].split(".")[-1],
            "score": hit["final_score"],
            "role": md.get("role", "attribute"),
            "priority": int(md.get("priority", hit["priority"])),
            "text": hit["text"],
        })
        if tname not in table_set and len(selected_tables) < top_k_tables:
            tinfo = semantic["tables"].get(tname, {})
            selected_tables.append({
                "table": tname,
                "business_name": tinfo.get("business_name", tname),
                "table_type": tinfo.get("table_type", "unknown"),
                "score": hit["final_score"] - 0.01,
                "summary": tinfo.get("table_summary", ""),
                "available_metrics": tinfo.get("available_metrics", []),
                "row_count": tinfo.get("row_count", 0),
            })
            table_set.add(tname)

    # --- completeness: never hide a table the answer may need ----------------------------------------------------
    def _add_table(tname: str, score: float, reason: str) -> None:
        tinfo = semantic["tables"].get(tname, {})
        selected_tables.append({
            "table": tname,
            "business_name": tinfo.get("business_name", tname),
            "table_type": tinfo.get("table_type", "unknown"),
            "score": score,
            "summary": tinfo.get("table_summary", ""),
            "available_metrics": tinfo.get("available_metrics", []),
            "row_count": tinfo.get("row_count", 0),
            "included_because": reason,
        })
        table_set.add(tname)

    best_score: Dict[str, float] = {}
    for hit in raw_hits:
        if hit.get("table"):
            best_score[hit["table"]] = max(best_score.get(hit["table"], 0.0), hit["final_score"])
    tables_info = semantic.get("tables") or {}
    non_empty = [t for t, info in tables_info.items() if info.get("row_count")]
    total_columns = sum(len(tables_info[t].get("columns", [])) for t in non_empty)
    if total_columns <= SMALL_SCHEMA_COLUMNS:
        for tname in sorted(non_empty, key=lambda t: -best_score.get(t, 0.0)):
            if tname not in table_set:
                _add_table(tname, best_score.get(tname, 0.0) - 0.02, "small schema: every non-empty table is shown")
    else:
        graph = _relationship_graph(semantic)
        chosen = [t["table"] for t in selected_tables]
        for i, a in enumerate(chosen):
            for b in chosen[i + 1:]:
                for mid in _path(graph, a, b)[1:-1]:
                    if mid not in table_set and len(selected_tables) < MAX_JOIN_PATH_TABLES:
                        _add_table(mid, best_score.get(mid, 0.0) - 0.02, f"connects {a} and {b}")

    for hit in metric_hits:
        mname = hit.get("metadata", {}).get("metric")
        if mname and len([t for t in selected_tables if not t.get("included_because")]) < top_k_tables:
            selected_tables.append({
                "table": f"metric::{mname}",
                "business_name": mname,
                "table_type": "metric",
                "score": hit["final_score"],
                "summary": hit["text"],
                "available_metrics": [mname],
                "row_count": 0,
            })

    selected_tables.sort(key=lambda x: (x["score"], x["row_count"]), reverse=True)
    selected_columns.sort(key=lambda x: (x["score"], x["priority"]), reverse=True)

    relevant_tables = [t["table"] for t in selected_tables if not str(t["table"]).startswith("metric::")]
    relevant_columns_by_table: Dict[str, List[dict]] = {}
    for col in selected_columns:
        relevant_columns_by_table.setdefault(col["table"], []).append(col)

    join_hints = []
    seen: set = set()
    for hit in rel_hits:
        rel = hit.get("metadata", {}).get("relationship") or hit.get("text", "")
        if rel and rel not in seen:
            seen.add(rel)
            join_hints.append(rel)

    context_text_lines = ["RETRIEVED CONTEXT:", "", "Top Tables:"]
    for t in selected_tables:
        context_text_lines.append(f"- {t['table']} ({t['business_name']}, {t['table_type']}, score={t['score']:.3f})")
        context_text_lines.append(f"  Summary: {t['summary']}")
        context_text_lines.append(f"  Metrics: {', '.join(t['available_metrics']) or 'general'}")
    context_text_lines.append("")
    context_text_lines.append("Top Columns:")
    for tname, cols in relevant_columns_by_table.items():
        context_text_lines.append(f"- {tname}:")
        for c in cols:
            context_text_lines.append(f"  • {c['column']} (role={c['role']}, priority={c['priority']}, score={c['score']:.3f})")
    if join_hints:
        context_text_lines.append("")
        context_text_lines.append("Join Hints:")
        for j in join_hints[:8]:
            context_text_lines.append(f"- {j}")

    return {
        "question": question,
        "top_tables": selected_tables,
        "top_columns": selected_columns,
        "relevant_tables": relevant_tables,
        "relevant_columns_by_table": relevant_columns_by_table,
        "join_hints": join_hints,
        "retrieved_context_text": "\n".join(context_text_lines),
        "document_count": len(docs),
        "vector_query": raw_hits[:20],
        "model_name": "tfidf",
        "vector_store": semantic.get("vector_store", {}),
    }


def retrieval_to_prompt_text(retrieval: dict) -> str:
    lines = ["RAG RETRIEVAL OUTPUT:\n"]
    lines.append(f"Question: {retrieval['question']}")
    lines.append("")
    lines.append("Relevant Tables:")
    for t in retrieval.get("top_tables", []):
        lines.append(f"- {t['table']} ({t['business_name']}) score={t['score']:.3f}")
    lines.append("")
    lines.append("Relevant Columns:")
    for table, cols in retrieval.get("relevant_columns_by_table", {}).items():
        lines.append(f"- {table}:")
        for c in cols:
            lines.append(f"  • {c['column']} role={c['role']} priority={c['priority']} score={c['score']:.3f}")
    if retrieval.get("join_hints"):
        lines.append("")
        lines.append("Join Hints:")
        for j in retrieval["join_hints"]:
            lines.append(f"- {j}")
    lines.append("")
    lines.append("Retrieved Context Text:")
    lines.append(retrieval.get("retrieved_context_text", ""))
    return "\n".join(lines)
