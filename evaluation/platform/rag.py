"""Retrieval evaluation: runs the real ``knowledge.search`` (chunking, TF-IDF/cosine, score floor) over the fixed corpus."""
from __future__ import annotations

from typing import Dict, List

import config
import knowledge

from .corpus import QUESTIONS, VECTOR_INDEX_THRESHOLDS

KS = (1, 3, 6)


def _rate(n: int, d: int):
    return None if d == 0 else round(n / d, 3)


def wilson(k: int, n: int, z: float = 1.96):
    """95% Wilson interval for k successes in n trials -- with a corpus this small a point estimate alone would overstate certainty."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)


def run(env) -> dict:
    per_class: Dict[str, dict] = {}
    misses: List[dict] = []
    for cls, question, gold_doc, marker in QUESTIONS:
        found = knowledge.search(env.db, env.ws.id, question, top_k=max(KS))
        agg = per_class.setdefault(cls, {"n": 0, "hits": {k: 0 for k in KS}, "rr": 0.0, "returned": 0})
        agg["n"] += 1
        agg["returned"] += 1 if found else 0
        if cls == "offtopic":
            continue
        rank = next((i for i, p in enumerate(found, 1) if p["document_name"] == gold_doc and marker in p["text"].lower()), None)
        for k in KS:
            agg["hits"][k] += 1 if rank and rank <= k else 0
        agg["rr"] += 1 / rank if rank else 0.0
        if rank is None or rank > 3:
            misses.append({"class": cls, "question": question, "gold": gold_doc, "rank": rank,
                           "top": [(p["document_name"], p["score"]) for p in found[:2]]})
    classes = {}
    for cls, a in per_class.items():
        if cls == "offtopic":
            classes[cls] = {"n": a["n"], "false_positive_rate": _rate(a["returned"], a["n"])}
        else:
            classes[cls] = {"n": a["n"], **{f"recall@{k}": _rate(a["hits"][k], a["n"]) for k in KS}, "mrr": round(a["rr"] / a["n"], 3)}
    t = VECTOR_INDEX_THRESHOLDS
    ci = {c: wilson(per_class[c]["hits"][3], per_class[c]["n"]) for c in ("keyword", "paraphrase")}
    below = lambda c, floor: classes[c]["recall@3"] < floor  # noqa: E731
    clearly = lambda c, floor: ci[c][1] < floor  # noqa: E731
    if clearly("keyword", t["keyword_recall_at_3_min"]) or clearly("paraphrase", t["paraphrase_recall_at_3_min"]) or classes["offtopic"]["false_positive_rate"] > t["offtopic_false_positive_max"]:
        decision = "justified"
    elif below("keyword", t["keyword_recall_at_3_min"]) or below("paraphrase", t["paraphrase_recall_at_3_min"]):
        decision = "inconclusive"
    else:
        decision = "not_justified"
    verdict = {
        "keyword_recall@3": classes["keyword"]["recall@3"], "paraphrase_recall@3": classes["paraphrase"]["recall@3"],
        "offtopic_false_positive_rate": classes["offtopic"]["false_positive_rate"],
        "thresholds": t,
        "recall@3_95pct_interval": ci,
        "vector_index": decision,
        "rule": "justified = a recall interval lies wholly below its threshold (or off-topic false positives exceed the cap); "
                "inconclusive = point estimate below the threshold but the interval still contains it; else not_justified",
    }
    return {"suite": "retrieval", "chunk_chars": config.KNOWLEDGE_CHUNK_CHARS,
            "min_score": config.KNOWLEDGE_MIN_SCORE, "top_k_default": config.KNOWLEDGE_TOP_K, "classes": classes, "misses": misses, "decision": verdict}
