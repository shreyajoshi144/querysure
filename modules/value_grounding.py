"""Exact-value grounding for categorical filters.

Root causes this addresses (CSV, but applies to any text column):
  * the profile used to cut values at 40 chars and the prompt builder clipped every line at 400,
    so a long value could never be reproduced exactly in a WHERE clause;
  * only the first 25 distinct values were ever shown, so any other value was invisible;
  * whitespace inside values was collapsed by the prompt sanitiser.
Here the question is matched against the REAL distinct values in the dataset (read-only), and the
exact stored spelling is handed to the generator (and to the validator) -- nothing is hard-coded.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MAX_DISTINCT_FOR_GROUNDING = 20000
MAX_VALUE_LEN = 1000          # values longer than this are reported as ungroundable instead of being cut
_FUZZY_CUTOFF = 0.88
_SHORT_VALUE_LEN = 3          # values this short must appear in the question with their stored case
_MANY_VALUES = 200            # columns with more distinct values than this are treated as free text (names, titles)
_MAX_FUZZY_VALUES = 5000

_cache: Dict[Tuple[str, float, str, str], List[str]] = {}


def norm(text: Any) -> str:
    t = unicodedata.normalize("NFKD", str(text))
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).casefold()  # accents do not distinguish values
    t = re.sub(r"[\s ]+", " ", t).strip()
    return t.strip(" \t.,;:!?\"'`()[]{}")


def sanitize_for_prompt(value: Any) -> str:
    """Prompt-safe JSON string that keeps the value EXACT (no truncation, whitespace preserved)."""
    text = "" if value is None else str(value)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "Cn") or ch in "\t")
    text = text.replace("<<<", "‹‹‹").replace(">>>", "›››")
    return json.dumps(text, ensure_ascii=False)


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _distinct_values(db_path: str, table: str, column: str) -> List[str]:
    try:
        mtime = os.path.getmtime(db_path)
    except OSError:
        return []
    key = (db_path, mtime, table, column)
    if key in _cache:
        return _cache[key]
    con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = con.execute(f"SELECT DISTINCT {_q(column)} FROM {_q(table)} WHERE {_q(column)} IS NOT NULL LIMIT {MAX_DISTINCT_FOR_GROUNDING + 1}").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    vals = [str(r[0]) for r in rows if isinstance(r[0], str)]
    if len(_cache) > 256:
        _cache.clear()
    _cache[key] = vals
    return vals


def _text_columns(schema: dict):
    for tname, tinfo in (schema.get("tables") or {}).items():
        stats = tinfo.get("column_stats") or {}
        prof = tinfo.get("column_profile") or {}
        for col in tinfo.get("columns", []):
            if (prof.get(col["name"]) or {}).get("kind") == "date_text":
                continue  # dates are handled by the date-range planner, not as categorical values
            if any(k in str(col.get("type", "")).upper() for k in ("CHAR", "TEXT", "CLOB")) or not col.get("type"):
                d = (stats.get(col["name"]) or {}).get("distinct", 0)
                if 0 < d <= MAX_DISTINCT_FOR_GROUNDING:
                    yield tname, col["name"]


def _ngrams(words: List[str], n: int):
    for i in range(len(words) - n + 1):
        yield " ".join(words[i:i + n])


def ground_question(question: str, schema: dict, db_path: Optional[str]) -> List[Dict[str, Any]]:
    """Find dataset values mentioned in the question. Returns exact stored spellings.

    Each hit: {table, column, value, match: 'exact'|'fuzzy', mentioned: <text in question>}.
    Longest match wins when one value is contained in another ('Premium' vs 'Premium Tier (Annual)')."""
    if not question or not db_path or not os.path.exists(db_path):
        return []
    qn = norm(question)
    qwords = qn.split()
    hits: List[Dict[str, Any]] = []
    for table, column in _text_columns(schema):
        values = _distinct_values(db_path, table, column)
        if not values:
            continue
        # In a large free-text column (track titles, product names) a one-word value such as 'Seconds', 'Money' or
        # 'Time' is far more likely to be an ordinary word of the question; ground it only when written as stored.
        many_values = len(values) > _MANY_VALUES
        col_hits = []
        for v in values:
            if len(v) > MAX_VALUE_LEN:
                continue
            vn = norm(v)
            if len(vn) < 2 or vn == norm(column):
                continue
            if len(vn) <= _SHORT_VALUE_LEN or (many_values and " " not in vn):
                # Short stored values ('IS', 'DE', 'NO', 'War') collide with everyday words in many languages ("is", "de",
                # "no", German "war"). Ground them only when the question writes them exactly as stored (same case).
                if re.search(r"(?<![\w])" + re.escape(v.strip()) + r"(?![\w])", question):
                    col_hits.append({"table": table, "column": column, "value": v, "match": "exact", "mentioned": vn, "_len": len(vn)})
                continue
            if re.search(r"(?<![\w])" + re.escape(vn) + r"(?![\w])", qn):
                col_hits.append({"table": table, "column": column, "value": v, "match": "exact", "mentioned": vn, "_len": len(vn)})
        if not col_hits and len(values) <= _MAX_FUZZY_VALUES:
            vn_map = {}
            for v in values:
                if len(v) <= MAX_VALUE_LEN:
                    vn_map.setdefault(norm(v), v)
            by_words: Dict[int, List[str]] = {}
            for vn in vn_map:
                by_words.setdefault(len(vn.split()), []).append(vn)
            for n, cands in by_words.items():
                if n > 8 or n > len(qwords):
                    continue
                for gram in set(_ngrams(qwords, n)):
                    if len(gram) < 4:
                        continue
                    m = difflib.get_close_matches(gram, cands, n=1, cutoff=_FUZZY_CUTOFF)
                    if m and gram != m[0]:
                        col_hits.append({"table": table, "column": column, "value": vn_map[m[0]], "match": "fuzzy",
                                         "mentioned": gram, "_len": len(m[0])})
        hits.extend(col_hits)
    # drop hits whose mention is fully contained in a longer hit's mention (same column)
    keep = []
    for h in hits:
        dominated = any(o is not h and o["table"] == h["table"] and o["column"] == h["column"]
                        and o["_len"] > h["_len"] and h["mentioned"] in o["mentioned"] for o in hits)
        if not dominated:
            keep.append(h)
    # Several stored values can normalise identically ('Smith  and  Co' / 'smith and co'): when the question contains one
    # of them VERBATIM, that is the one meant; otherwise keep all candidates and let the generator/validator see the ambiguity.
    final = []
    for h in keep:
        same = [o for o in keep if o["table"] == h["table"] and o["column"] == h["column"] and o["mentioned"] == h["mentioned"]]
        verbatim = [o for o in same if o["value"] in question]
        if len(same) > 1 and verbatim and h not in verbatim:
            continue
        final.append(h)
    for h in final:
        h.pop("_len", None)
        h["ambiguous"] = any(o is not h and o["table"] == h["table"] and o["column"] == h["column"] and o["mentioned"] == h["mentioned"] for o in final)
    return final


def grounded_block_text(hits: List[Dict[str, Any]]) -> str:
    if not hits:
        return ""
    lines = ["GROUNDED FILTER VALUES (exact stored spelling; copy verbatim into WHERE, do not change case, spacing or punctuation):"]
    for h in hits:
        tag = "" if h["match"] == "exact" else f"  [closest stored value to the question's {sanitize_for_prompt(h['mentioned'])}]"
        lines.append(f" {h['table']}.{h['column']} = {sanitize_for_prompt(h['value'])}{tag}")
    return "\n".join(lines)


def value_exists(db_path: str, table: str, column: str, value: str) -> bool:
    con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return con.execute(f"SELECT 1 FROM {_q(table)} WHERE {_q(column)} = ? LIMIT 1", (value,)).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        con.close()


def nearest_values(db_path: str, table: str, column: str, literal: str, n: int = 3) -> List[str]:
    vals = _distinct_values(db_path, table, column)
    ln = norm(literal)
    exact_norm = [v for v in vals if norm(v) == ln]
    if exact_norm:
        return exact_norm[:n]
    by_norm = {norm(v): v for v in vals}
    # a value cut short (prefix/substring of a stored value) is the most common failure: rank those first
    if len(ln) >= 8:
        contained = [by_norm[k] for k in sorted(by_norm) if k.startswith(ln) or ln in k]
        if contained:
            return contained[:n]
    close = difflib.get_close_matches(ln, list(by_norm), n=n, cutoff=0.75)
    return [by_norm[c] for c in close]
