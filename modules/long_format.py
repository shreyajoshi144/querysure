"""Long-format ("tidy statistical") tables: one row per combination of a few key columns plus one value column.

Official statistics exports (e.g. Stats NZ business data) store every figure as a row keyed by period, entity and a few
qualifier columns: a hierarchy level ("NZSIOC Level 1" / "Level 2"), a version ("Unadjusted" / "Seasonally adjusted" /
"Trend") and a variable ("Sales", "Salaries and wages", ...). Summing the value column without fixing the level or the
version counts the same money several times, and an entity that exists at two levels ("Mining") appears twice.

Everything here is derived from the data, never from column names or known datasets:

* the table has no row identifier, and a combination of low-cardinality columns identifies every row (the key);
* a key column whose values each add up to the same totals while covering different numbers of entities is a
  HIERARCHY LEVEL (the coarsest level, with the fewest entities, is the top level);
* a key column whose values give the same per-series totals over time is a set of alternative VERSIONS of one figure;
* any other few-valued key column is a QUALIFIER (different variables): it is described, not enforced, because a
  few-valued column can also be a legitimately additive dimension (region, channel).
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional

MAX_ROWS = 1_000_000            # profiling cost bound (the ingestion row cap)
MIN_ROWS = 20
MAX_QUALIFIER_VALUES = 12        # a hierarchy level / version / variable column has few values
MAX_KEY_DISTINCT_SHARE = 0.2     # key candidates repeat; a measure (7,103 distinct of 9,555) does not
MAX_KEY_DISTINCT = 1000
SAME_TOTAL_TOLERANCE = 0.01      # hierarchy: every level sums to the same total (rounding only)
SAME_SERIES_TOLERANCE = 0.02     # versions: adjusted and unadjusted series agree over the whole period
MIN_AGREEING_SHARE = 0.9

_COARSE_WORDS = re.compile(r"\b(top[\s-]*level|level\s*(?:1|one)|high[\s-]*level|broad(?:est)?|main|major|overall|"
                           r"parent|aggregate[sd]?|headline|nivel superior|primer nivel|premier niveau|obersten ebene|"
                           r"oberste ebene|शीर्ष)", re.I)
_FINE_WORDS = re.compile(r"\b(sub[\s-]*(?:industr\w*|categor\w*|sector\w*|level\w*)|level\s*(?:2|two)|detailed|detail|"
                         r"granular|lower[\s-]*level|child|breakdown within)", re.I)
_LEVEL_N = re.compile(r"\blevel\s*(\d)\b", re.I)


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _is_numeric(col: Dict[str, Any]) -> bool:
    t = str(col.get("type") or "").upper()
    return any(k in t for k in ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC"))


def _unique(conn: sqlite3.Connection, table: str, cols: List[str], rows: int) -> bool:
    if not cols:
        return False
    sel = ", ".join(_q(c) for c in cols)
    n = conn.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT {sel} FROM {_q(table)})").fetchone()[0]
    return int(n) == rows


def _agreeing_share(conn: sqlite3.Connection, table: str, measure: str, q: str, by: List[str], tol: float,
                    common_over: Optional[List[str]] = None) -> Optional[float]:
    """Share of `by` groups (where >= 2 values of q are present) whose per-value totals of `measure` agree within tol.
    With `common_over` (the time columns), totals only cover the time points at which every value of q is present, so
    an adjusted series that starts later is compared with the same quarters of the unadjusted one."""
    by_sql = ", ".join(_q(c) for c in by)
    src = _q(table)
    if common_over:
        keys = ", ".join(_q(c) for c in by + common_over)
        n_q = int(conn.execute(f"SELECT COUNT(DISTINCT {_q(q)}) FROM {_q(table)}").fetchone()[0])
        src = (f"(SELECT t.* FROM {_q(table)} AS t JOIN (SELECT {keys} FROM {_q(table)} WHERE {_q(measure)} IS NOT NULL "
               f"GROUP BY {keys} HAVING COUNT(DISTINCT {_q(q)}) = {n_q}) AS c USING ({keys}))")
    inner = f"SELECT {by_sql + ', ' if by else ''}{_q(q)} AS qv, SUM({_q(measure)}) AS s FROM {src} " \
            f"WHERE {_q(measure)} IS NOT NULL GROUP BY {by_sql + ', ' if by else ''}{_q(q)}"
    grp = f"GROUP BY {by_sql}" if by else ""
    rows = conn.execute(f"SELECT COUNT(*) AS k, MIN(s), MAX(s), MAX(ABS(s)) FROM ({inner}) {grp}").fetchall()
    comparable = [r for r in rows if r[0] >= 2]
    if not comparable:
        return None
    ok = sum(1 for _, lo, hi, mag in comparable if (hi - lo) <= tol * (mag or 0) + 1e-9)
    return ok / len(comparable)


def detect(conn: sqlite3.Connection, table: str, columns: List[Dict[str, Any]], stats: Dict[str, Any],
           row_count: int) -> Optional[Dict[str, Any]]:
    """Structure of a long-format table, or None. Read-only; bounded by MAX_ROWS."""
    if not (MIN_ROWS <= row_count <= MAX_ROWS):
        return None
    st = {c["name"]: stats.get(c["name"], {}) for c in columns}
    if any(s.get("distinct") == s.get("non_null") == row_count and row_count for s in st.values()):
        return None                                   # an identifier column: an ordinary table of records
    limit = min(MAX_KEY_DISTINCT, max(MAX_QUALIFIER_VALUES, int(row_count * MAX_KEY_DISTINCT_SHARE)))
    cands = [c["name"] for c in columns if 2 <= st[c["name"]].get("distinct", 0) <= limit]
    try:
        if not _unique(conn, table, cands, row_count):
            return None
        avg_len = {c: float(conn.execute(f"SELECT AVG(LENGTH(CAST({_q(c)} AS TEXT))) FROM {_q(table)}").fetchone()[0] or 0)
                   for c in cands}
        key = list(cands)
        # Drop codes and annotations first, and of two columns carrying the same information keep the more descriptive
        # one ('Industry by financial variable (NZSIOC Level 1)' rather than 'Current').
        for c in sorted(cands, key=lambda n: (-st[n].get("distinct", 0), avg_len[n])):
            rest = [k for k in key if k != c]
            if _unique(conn, table, rest, row_count):
                key = rest
        by_name = {c["name"]: c for c in columns}
        measures = [c["name"] for c in columns if c["name"] not in key and _is_numeric(c)
                    and st[c["name"]].get("distinct", 0) > MAX_QUALIFIER_VALUES]
        text_key = sorted((k for k in key if not _is_numeric(by_name[k])), key=lambda n: -st[n].get("distinct", 0))
        if not measures or len(text_key) < 2:
            return None
        # the entity (industry, region, ...) is the text key with the most values; the other few-valued ones qualify it
        measure, entity = measures[0], text_key[0]
        qualifiers = [k for k in text_key[1:] if st[k].get("distinct", 0) <= MAX_QUALIFIER_VALUES]
        if not qualifiers:
            return None
        time_cols = [k for k in key if k not in qualifiers and k != entity]
        out: Dict[str, Any] = {"key": key, "measure": measure, "measures": measures, "entity": entity,
                               "time": time_cols, "columns": []}
        for q in qualifiers:
            vals = [r[0] for r in conn.execute(
                f"SELECT {_q(q)}, COUNT(*) AS n FROM {_q(table)} WHERE {_q(q)} IS NOT NULL GROUP BY {_q(q)} ORDER BY n DESC, {_q(q)}")]
            ents = {v: n for v, n in conn.execute(
                f"SELECT {_q(q)}, COUNT(DISTINCT {_q(entity)}) FROM {_q(table)} WHERE {_q(q)} IS NOT NULL GROUP BY {_q(q)}")}
            others = [k for k in key if k not in (q, entity)]
            same_totals = _agreeing_share(conn, table, measure, q, others, SAME_TOTAL_TOLERANCE)
            kind = "qualifier"
            if same_totals is not None and same_totals >= MIN_AGREEING_SHARE and len(set(ents.values())) > 1:
                kind = "hierarchy_level"
            elif time_cols:
                per_series = _agreeing_share(conn, table, measure, q, [k for k in key if k != q and k not in time_cols],
                                             SAME_SERIES_TOLERANCE, common_over=time_cols)
                if per_series is not None and per_series >= MIN_AGREEING_SHARE:
                    kind = "versions"
            entry: Dict[str, Any] = {"column": q, "kind": kind, "values": [str(v) for v in vals],
                                     "aliases": _aliases(conn, table, q, columns, st)}
            if kind == "hierarchy_level":
                levels = sorted(vals, key=lambda v: (ents.get(v, 0), str(v)))
                entry["levels"] = [str(v) for v in levels]
                entry["entities_per_level"] = {str(v): int(ents.get(v, 0)) for v in levels}
                shared = conn.execute(
                    f"SELECT COUNT(*) FROM (SELECT {_q(entity)} FROM {_q(table)} GROUP BY {_q(entity)} "
                    f"HAVING COUNT(DISTINCT {_q(q)}) > 1)").fetchone()[0]
                entry["entities_in_several_levels"] = int(shared)
            if kind == "versions":
                entry["default"] = str(vals[0])        # the most complete series (most rows)
            out["columns"].append(entry)
        if not any(e["kind"] in ("hierarchy_level", "versions") for e in out["columns"]):
            return None
        return out
    except sqlite3.Error:
        return None


def _aliases(conn: sqlite3.Connection, table: str, q: str, columns: List[Dict[str, Any]], st: Dict[str, Any]) -> List[str]:
    """Other columns that split the rows exactly like q (a 1:1 relabelling): fixing either one fixes both."""
    n = st[q].get("distinct", 0)
    out = []
    for c in columns:
        c = c["name"]
        if c == q or st[c].get("distinct", 0) != n:
            continue
        pairs = conn.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT {_q(q)}, {_q(c)} FROM {_q(table)})").fetchone()[0]
        if int(pairs) == n:
            out.append(c)
    return out


# --------------------------------------------------------------------------------------------- question -> filters
def _level_from_question(question: str, levels: List[str]) -> Optional[str]:
    m = _LEVEL_N.search(question or "")
    if m:
        for v in levels:                                 # "level 2" names a value that says "Level 2"
            if re.search(r"\blevel\s*" + m.group(1) + r"\b", v, re.I):
                return v
    if _FINE_WORDS.search(question or ""):
        return levels[-1]
    if _COARSE_WORDS.search(question or ""):
        return levels[0]
    return None


_WORD = re.compile(r"[^\W_]+", re.U)
_GENERIC = {"and", "the", "of", "for", "in", "or", "a", "an", "to", "by", "per", "total", "value", "values", "current",
            "prices", "price", "level", "all", "other", "and"}


def _stem(w: str) -> str:
    w = w.lower()
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _named_values(question: str, values: List[str]) -> List[str]:
    """Stored values the question names by a word that belongs to that value only ('sales' -> 'Sales (operating
    income)'; 'operating' is shared by two values and names neither)."""
    words = {_stem(w) for w in _WORD.findall(question or "")}
    toks = {v: {_stem(w) for w in _WORD.findall(v)} - _GENERIC for v in values}
    counts: Dict[str, int] = {}
    for t in toks.values():
        for w in t:
            counts[w] = counts.get(w, 0) + 1
    return [v for v in values if any(counts[w] == 1 and w in words for w in toks[v])]


def required(question: str, schema: dict, table: str, grounded: Optional[list] = None,
             db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """For a question about a long-format table: the hierarchy/version columns the SQL must fix to ONE value, each with
    the values that are acceptable for this question ("choices") and the one a reference query should use ("value")."""
    info = ((schema.get("tables") or {}).get(table) or {}).get("long_format")
    if not info:
        return []
    named = {}
    for h in grounded or []:
        if h.get("table") == table and h.get("match") == "exact":
            named.setdefault(h["column"], set()).add(str(h["value"]))
    out = []
    for col in info["columns"]:
        c = col["column"]
        if col["kind"] == "versions":
            if named.get(c):
                vals = sorted(named[c])
                out.append({"column": c, "kind": "versions", "choices": vals, "value": vals[0], "from": "question"})
            else:
                out.append({"column": c, "kind": "versions", "choices": list(col["values"]), "value": col["default"],
                            "from": "default"})
        elif col["kind"] == "qualifier":
            vals = sorted(named[c]) if named.get(c) else _named_values(question, col["values"])
            if vals:      # only enforced when the question names a value: a few-valued column may also be additive
                out.append({"column": c, "kind": "qualifier", "choices": vals, "value": vals[0], "from": "question"})
        elif col["kind"] == "hierarchy_level":
            levels = col["levels"]
            if named.get(c):
                vals = sorted(named[c])
                out.append({"column": c, "kind": "hierarchy_level", "choices": vals, "value": vals[0], "from": "question"})
                continue
            asked = _level_from_question(question, levels)
            ents = named.get(info["entity"])
            if asked:
                out.append({"column": c, "kind": "hierarchy_level", "choices": [asked], "value": asked, "from": "question"})
            elif ents and db_path:
                # "Mining": any level that contains the named entity gives the same figures
                have = _levels_containing(db_path, table, c, info["entity"], sorted(ents))
                choices = [v for v in levels if v in have] or list(levels)
                out.append({"column": c, "kind": "hierarchy_level", "choices": choices, "value": choices[0],
                            "from": "entity"})
            else:
                out.append({"column": c, "kind": "hierarchy_level", "choices": list(levels), "value": levels[0],
                            "from": "default"})
    return out


def _levels_containing(db_path: str, table: str, level_col: str, entity_col: str, entities: List[str]) -> set:
    try:
        from pathlib import Path
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            marks = ", ".join("?" for _ in entities)
            rows = conn.execute(f"SELECT DISTINCT {_q(level_col)} FROM {_q(table)} WHERE {_q(entity_col)} IN ({marks})",
                                entities).fetchall()
            return {str(r[0]) for r in rows}
        finally:
            conn.close()
    except sqlite3.Error:
        return set()


# ------------------------------------------------------------------------------------------------------ SQL check
def _uses_values(tree, measure: str) -> bool:
    """The SQL reads the measure's values (aggregates other than COUNT, the select list, ORDER BY), not only tests them
    for NULL or counts rows."""
    from sqlglot import exp
    m = measure.lower()
    for col in tree.find_all(exp.Column):
        if col.name.lower() != m:
            continue
        p, inside_count, in_null_test = col.parent, False, False
        while p is not None:
            if isinstance(p, exp.Count):
                inside_count = True
            if isinstance(p, exp.Is):
                in_null_test = True
            if isinstance(p, (exp.Select, exp.Order)):
                break
            p = p.parent
        if not inside_count and not in_null_test:
            return True
    return False


def check_sql(question: str, sql: str, schema: dict, grounded: Optional[list] = None,
              db_path: Optional[str] = None) -> List[Dict[str, str]]:
    """Issues for SQL that adds up a long-format table's values across hierarchy levels or versions."""
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:  # noqa: BLE001 - advisory check only
        return []
    tables = {t.name.lower() for t in tree.find_all(exp.Table)}
    issues: List[Dict[str, str]] = []
    for tname, tinfo in (schema.get("tables") or {}).items():
        info = tinfo.get("long_format")
        if not info or tname.lower() not in tables or not _uses_values(tree, info["measure"]):
            continue
        grouped = {c.name.lower() for g in tree.find_all(exp.Group) for c in g.find_all(exp.Column)}
        by_col = {c["column"]: c for c in info["columns"]}
        levels = {c["column"].lower() for c in info["columns"] if c["kind"] == "hierarchy_level"}
        dims = grouped - {info["measure"].lower()}
        # "compare the levels", "by level", "each level" asks for levels; "top-level industries" names one level
        asks_levels = bool(re.search(r"\b(?:levels|niveles|niveaux|ebenen)\b|\b(?:by|per|each|every|across|which|por|par|je)\s+"
                                     r"(?:level|nivel|niveau|ebene)\b", question or "", re.I))
        if dims and dims <= levels and not asks_levels:
            col = sorted(dims)[0]
            issues.append({"type": "long_format_level_grouping",
                           "message": f"{col} is a hierarchy level, not a category to compare: grouping by it returns one row per "
                                      f"level. Group by {info['entity']} and filter {col} to one level instead."})
        outer = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
        projected = {(p.this if isinstance(p, exp.Alias) else p).name.lower() for p in (outer.expressions if outer else [])
                     if isinstance(p.this if isinstance(p, exp.Alias) else p, exp.Column)}
        label_levels = projected & (levels | {a.lower() for c in info["columns"] if c["kind"] == "hierarchy_level"
                                              for a in c.get("aliases", [])})
        if label_levels and info["entity"].lower() not in projected and not asks_levels:
            col = sorted(label_levels)[0]
            issues.append({"type": "long_format_level_label",
                           "message": f"{col} holds the hierarchy level, not the {info['entity']} name: select {info['entity']} "
                                      f"to say which one it is, and filter {col} to one level."})
        for req in required(question, schema, tname, grounded, db_path):
            col = req["column"].lower()
            if req["from"] == "entity" and len(req["choices"]) == 1:
                continue                                  # the named entity exists at one level only: already fixed
            aliases = {a.lower() for a in by_col.get(req["column"], {}).get("aliases", [])}
            if ({col} | aliases) & grouped:
                continue
            cmp_nodes = list(tree.find_all(exp.EQ)) + list(tree.find_all(exp.In)) + list(tree.find_all(exp.Like))
            nodes = [n for n in cmp_nodes if any(c.name.lower() == col for c in n.find_all(exp.Column))]
            if not nodes and any(any(c.name.lower() in aliases for c in n.find_all(exp.Column)) for n in cmp_nodes):
                continue                                  # fixed through an equivalent column
            lits = [lit.this for n in nodes for lit in n.find_all(exp.Literal) if lit.is_string]
            shown = ", ".join(repr(v) for v in req["choices"][:3])
            if not nodes:
                if req["kind"] == "qualifier":
                    msg = (f"{tname}.{req['column']} holds different variables; the question asks about {shown}, but the "
                           f"SQL does not filter {req['column']}, so it adds different variables together.")
                elif req["kind"] == "hierarchy_level":
                    msg = (f"{tname}.{info['measure']} repeats the same totals at every level of {req['column']}; the SQL "
                           f"does not fix {req['column']}, so it adds levels together (and counts entities present at "
                           f"several levels twice). Filter {req['column']} = {req['value']!r}"
                           + (" (the level the question asks for)." if req["from"] == "question" else
                              f" (or another single level: {shown})."))
                else:
                    msg = (f"{tname}.{req['column']} holds alternative versions of the same figures; the SQL does not fix "
                           f"it, so it adds them together. Filter {req['column']} = {req['value']!r}"
                           + (" (named in the question)." if req["from"] == "question" else " (the most complete version)."))
                issues.append({"type": f"long_format_{req['kind']}", "message": msg, "table": tname,
                               "column": req["column"], "value": req["value"], "single": len(req["choices"]) == 1
                               or req["from"] == "default"})
            elif lits and not any(v in req["choices"] for v in lits):
                issues.append({"type": f"long_format_{req['kind']}_value",
                               "message": f"{req['column']} is filtered to {lits[0]!r}, but the question asks for {shown}."})
    return issues


def add_missing_filters(sql: str, issues: List[Dict[str, Any]]) -> Optional[str]:
    """Deterministic repair: add `<alias>.<column> = '<value>'` to every SELECT that reads the table directly, for each
    missing level / version / variable filter whose value is unambiguous. None when nothing could be added."""
    todo = [i for i in issues if i.get("type") in ("long_format_hierarchy_level", "long_format_versions",
                                                     "long_format_qualifier") and i.get("single")]
    if not todo:
        return None
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:  # noqa: BLE001
        return None
    changed = False
    for sel in list(tree.find_all(exp.Select)):
        sources = [s for s in [sel.args.get("from")] + list(sel.args.get("joins") or []) if s is not None]
        for src in sources:
            t = src.this if isinstance(src.this, exp.Table) else None
            if t is None:
                continue
            for i in todo:
                if t.name.lower() != i["table"].lower():
                    continue
                alias = t.alias_or_name
                where = sel.args.get("where")
                if where is not None and any(c.name.lower() == i["column"].lower() for c in where.find_all(exp.Column)):
                    continue
                cond = exp.EQ(this=exp.column(i["column"], table=alias, quoted=True), expression=exp.Literal.string(i["value"]))
                sel.where(cond, copy=False)
                changed = True
    return tree.sql(dialect="sqlite") if changed else None


# --------------------------------------------------------------------------------------------------- prompt text
def prompt_lines(table: str, info: Dict[str, Any], clean=lambda s, n=200: str(s)[:n]) -> List[str]:
    key = ", ".join(clean(k, 64) for k in info["key"])
    lines = [f" LONG-FORMAT TABLE: one row per ({key}); {clean(info['measure'], 64)} is the figure for that combination."]
    for col in info["columns"]:
        c = clean(col["column"], 64)
        if col["kind"] == "hierarchy_level":
            lv = "; ".join(f"{clean(v)!r} ({col['entities_per_level'].get(v, 0)} {clean(info['entity'], 64)} values)"
                           for v in col["levels"])
            lines.append(f" - {c} is a HIERARCHY LEVEL, top level first: {lv}. Every level adds up to the same total"
                         + (f" and {col['entities_in_several_levels']} {clean(info['entity'], 64)} values appear at more than one level"
                            if col.get("entities_in_several_levels") else "")
                         + f". Always filter {c} to ONE level (the top level unless the question asks for a detailed or"
                           f" lower level); never add levels together.")
        elif col["kind"] == "versions":
            vals = ", ".join(repr(clean(v)) for v in col["values"])
            lines.append(f" - {c} holds alternative VERSIONS of the same figures ({vals}). Filter it to the version the"
                         f" question names, otherwise {clean(col['default'])!r}; never add versions together.")
        else:
            vals = ", ".join(repr(clean(v)) for v in col["values"])
            lines.append(f" - {c} distinguishes different variables or categories ({vals}): filter it to the one the"
                         f" question asks about, or use conditional aggregation to combine two.")
    return lines
