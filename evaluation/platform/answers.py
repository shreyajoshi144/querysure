"""Cited-answer quality (Sprint 13): retrieval -> the REAL prompt -> model reply -> the REAL answer validator.

Modes mirror ``agent_eval``: ``oracle`` (a reply built from the gold passage: a harness self-check: a perfect model, so the result is the retrieval ceiling), ``replay`` (raw replies
recorded from a live run, re-scored offline) and ``live`` (QuerySure's provider chain; costs money). Only live/replay numbers say anything about a model.

Per answerable question: did retrieval surface the gold passage, did the reply parse and cite, does a cited passage hold the gold fact
(``cites_gold``), and does the answer text state it (``states_fact``, a literal match on a short marker, so paraphrases count as misses: a floor).
Off-topic questions that still retrieve something are the risky ones: the right reply is ``insufficient`` and a grounded answer is a hallucination.
Questions where nothing is retrieved never reach a model (by design) and are counted as handled.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

import knowledge

from .corpus import QUESTIONS
from .rag import wilson


def _prompt(question: str, passages: List[dict]) -> str:
    return knowledge.build_prompt(question, passages)


def _live_reply(prompt: str, api_key: str):
    from ai import router as ai_router
    from modules import sql_generator as sg
    with ai_router.task_scope("knowledge_answer"):
        text, provider, _attempts = sg._run_provider_chain(knowledge.SYSTEM_PROMPT, prompt, api_key)
    return text, provider


def _oracle_reply(cls: str, passages: List[dict], marker: str) -> str:
    if cls == "offtopic":
        return json.dumps({"answer": "", "citations": [], "insufficient": True})
    n = next((i for i, p in enumerate(passages, 1) if marker in p["text"].lower()), None)
    if n is None:
        return json.dumps({"answer": "", "citations": [], "insufficient": True})
    return json.dumps({"answer": f"{marker} [{n}]", "citations": [n], "insufficient": False})


def run(env, mode: str = "oracle", responses: Optional[Dict[str, str]] = None, api_key: str = "", record: Optional[Dict[str, str]] = None,
        limit: int = 0) -> dict:
    rows: List[dict] = []
    providers = set()
    chosen = QUESTIONS if not limit else [q for q in QUESTIONS if q[0] != "offtopic"][: max(1, limit - 8)] + [q for q in QUESTIONS if q[0] == "offtopic"][:8]
    for idx, (cls, question, gold_doc, marker) in enumerate(chosen):
        qid = f"{cls}:{question}"
        passages = knowledge.search(env.db, env.ws.id, question)
        row = {"id": qid, "class": cls}
        if not passages:
            row.update(retrieved=False, model_called=False)
            rows.append(row)
            continue
        row["retrieved"] = True
        row["gold_in_context"] = cls != "offtopic" and any(p["document_name"] == gold_doc and marker in p["text"].lower() for p in passages)
        if mode == "oracle":
            reply = _oracle_reply(cls, passages, marker)
        elif mode == "replay":
            if qid not in (responses or {}):
                row["skipped"] = True
                rows.append(row)
                continue
            reply = responses[qid]
        else:
            try:
                reply, provider = _live_reply(_prompt(question, passages), api_key)
                providers.add(provider)
            except Exception as exc:  # noqa: BLE001 -- a provider failure is reported, never scored as a wrong answer
                row["provider_error"] = type(exc).__name__
                rows.append(row)
                continue
            if record is not None:
                record[qid] = reply
        row["model_called"] = True
        try:
            parsed = knowledge.parse_answer(reply, passages)
        except ValueError:
            row.update(parsed=False)
            rows.append(row)
            continue
        cited = [passages[c - 1] for c in parsed["citations"]]
        row.update(parsed=True, grounded=parsed["grounded"], insufficient=parsed["insufficient"], withheld=parsed["answer"] is None,
                   cites_gold=any(p["document_name"] == gold_doc and marker in p["text"].lower() for p in cited) if cls != "offtopic" else None,
                   states_fact=(marker in (parsed["answer"] or "").lower()) if cls != "offtopic" else None)
        rows.append(row)

    attempted = [r for r in rows if r.get("model_called") and "provider_error" not in r]
    ans = [r for r in attempted if r["class"] != "offtopic"]
    off = [r for r in attempted if r["class"] == "offtopic"]
    rate = lambda n, d: None if not d else round(n / d, 3)  # noqa: E731
    ci = lambda n, d: None if not d else [round(x, 3) for x in wilson(n, d)]  # noqa: E731
    parsed = [r for r in ans if r.get("parsed")]
    cg, sf = sum(1 for r in parsed if r.get("cites_gold")), sum(1 for r in parsed if r.get("states_fact"))
    halluc = sum(1 for r in off if r.get("parsed") and r.get("grounded"))
    return {"suite": "answers", "mode": mode, "questions": len(chosen), "providers": sorted(p for p in providers if p),
            "no_passage_so_no_model_call": sum(1 for r in rows if r.get("retrieved") is False),
            "provider_errors": sum(1 for r in rows if "provider_error" in r), "answerable_attempted": len(ans),
            "parse_rate": rate(len(parsed), len(ans)), "gold_in_context_rate": rate(sum(1 for r in ans if r.get("gold_in_context")), len(ans)),
            "cites_gold_rate": rate(cg, len(ans)), "cites_gold_interval": ci(cg, len(ans)),
            "states_fact_rate": rate(sf, len(ans)), "states_fact_interval": ci(sf, len(ans)),
            "withheld_rate": rate(sum(1 for r in ans if r.get("withheld")), len(ans)),
            "offtopic_attempted": len(off), "offtopic_grounded_answers": halluc, "offtopic_hallucination_rate": rate(halluc, len(off)),
            "rows": rows if mode != "oracle" else None}
