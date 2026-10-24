"""Deterministic question-vs-SQL intent checks.

The validator proves a query is SAFE and schema-correct; it cannot tell that a
query silently dropped a filter the user asked for. These checks catch the
failure modes seen on CSV/Excel data (missing filters, wrong percentage scale,
no ranking, wrong aggregate) and feed the existing repair loop. They only ever
produce advisory issues; they never relax validation.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_PCT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:%|percent\b|per cent\b)", re.I)
_TOPN_RE = re.compile(r"\b(?:top|bottom|first|last)\s+(\d+)\b", re.I)
_RANK_WORDS = re.compile(r"\b(top|bottom|highest|lowest|best|worst|most|least|largest|smallest|biggest|leading|number (?:one|1)|rank(?:ed|ing)?)\b", re.I)
_AVG_WORDS = re.compile(r"\b(average|avg|mean)\b", re.I)
_SUM_WORDS = re.compile(r"\b(total|sum)\b", re.I)
_COUNT_WORDS = re.compile(r"\b(how many|number of|counts? of|counts?)\b", re.I)
_SQL_LIT_NUM = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")


def _sql_literals(sql: str) -> set:
    out = set()
    for m in _SQL_LIT_NUM.findall(sql):
        try:
            out.add(round(float(m), 6))
        except ValueError:
            pass
    return out


def _profiles_used(sql: str, schema: Dict[str, Any]):
    low = sql.lower()
    for tinfo in schema.get("tables", {}).values():
        for cname, pr in (tinfo.get("column_profile") or {}).items():
            if re.search(r"(?<![a-z0-9_])" + re.escape(cname.lower()) + r"(?![a-z0-9_])", low):
                yield cname, pr


def _first_order_direction(sql: str):
    """'asc' / 'desc' of the first key of the outermost ORDER BY, or None when there is none."""
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read="sqlite")
        order = tree.args.get("order") or tree.find(exp.Order)
    except Exception:  # noqa: BLE001 - fall back to the text of the first key
        m = re.search(r"order\s+by\s+([^,;]+)", sql or "", flags=re.I)
        return None if not m else ("desc" if re.search(r"\bdesc\b", m.group(1), re.I) else "asc")
    if order is None or not order.expressions:
        return None
    return "desc" if order.expressions[0].args.get("desc") else "asc"


def _conditional_count(sql: str) -> bool:
    try:
        from .query_planner import _has_conditional_count
        import sqlglot
        return _has_conditional_count(sqlglot.parse_one(sql, read="sqlite"))
    except Exception:  # noqa: BLE001 - advisory check only
        return False


def _counts_a_measure(ql: str, sql: str) -> bool:
    """"total number of days required" / "how many hours were logged": the noun is a numeric column that is SUMmed."""
    from .query_planner import _stem, _tokens
    m = re.search(r"\b(?:how many|number of)\s+([a-z_ ]{3,40})", ql)
    if not m:
        return False
    noun = {_stem(t) for t in _tokens(m.group(1))}
    for arg in re.findall(r"\bsum\s*\(\s*([A-Za-z_][\w.\"]*)", sql or "", flags=re.I):
        col = arg.split(".")[-1].strip('"')
        if {_stem(t) for t in _tokens(col)} & noun:
            return True
    return False


def _is_key_column(schema: Dict[str, Any], name: str) -> bool:
    """A primary key or all-distinct column in some table: grouping by it fixes every other column of that row."""
    for tinfo in (schema.get("tables") or {}).values():
        for c in tinfo.get("columns", []):
            if c["name"].lower() == name.lower():
                st = (tinfo.get("column_stats") or {}).get(c["name"], {})
                if c.get("pk") or (st.get("rows") and st.get("distinct") == st.get("non_null") == st.get("rows")):
                    return True
    return False


def _ungrouped_columns(sql: str, schema: Dict[str, Any]) -> List[str]:
    """Columns selected next to GROUP BY that are neither grouped nor aggregated: SQLite returns the value of an arbitrary
    row of each group. Left alone: MIN()/MAX() queries (SQLite then returns the row holding the extreme, a documented
    pattern) and groups keyed by a primary/unique column (the other columns of that row are determined)."""
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:  # noqa: BLE001 - advisory check only
        return []
    out: List[str] = []
    for sel in tree.find_all(exp.Select):
        group = sel.args.get("group")
        if group is None or not group.expressions:
            continue
        if any(p.find(exp.Min, exp.Max) for p in sel.expressions):
            continue
        keys = group.expressions
        if any(isinstance(k, exp.Column) and _is_key_column(schema, k.name) for k in keys):
            continue
        key_names = {k.name.lower() for k in keys if isinstance(k, exp.Column)}
        key_sql = {k.sql().lower() for k in keys}
        positions = {int(k.name) for k in keys if isinstance(k, exp.Literal) and k.is_int}
        for i, p in enumerate(sel.expressions, start=1):
            inner = p.this if isinstance(p, exp.Alias) else p
            if not isinstance(inner, exp.Column) or i in positions:
                continue
            if inner.name.lower() in key_names or inner.sql().lower() in key_sql or p.alias_or_name.lower() in key_names:
                continue
            out.append(inner.name)
    return out


def _null_dropping_inequalities(sql: str, schema: Dict[str, Any]) -> List[tuple]:
    """`col <> 'Y'` on a mostly-NULL column silently removes the NULL rows (NULL <> 'Y' is not true), e.g. a 'suppressed'
    flag that is empty on 99.8% of rows turned every total into an empty result. Not flagged when the same query also
    tests the column for NULL."""
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:  # noqa: BLE001 - advisory check only
        return []
    null_pct: Dict[str, float] = {}
    for tinfo in (schema.get("tables") or {}).values():
        for cname, st in (tinfo.get("column_stats") or {}).items():
            null_pct[cname.lower()] = max(null_pct.get(cname.lower(), 0.0), float(st.get("null_pct") or 0.0))
    tested = {c.name.lower() for n in tree.find_all(exp.Is) for c in n.find_all(exp.Column)}
    out = []
    for node in list(tree.find_all(exp.NEQ)) + [n for n in tree.find_all(exp.Not) if isinstance(n.this, exp.In)]:
        for c in node.find_all(exp.Column):
            name = c.name.lower()
            if null_pct.get(name, 0.0) > 50.0 and name not in tested and (c.name, null_pct[name]) not in out:
                out.append((c.name, null_pct[name]))
    return out


def check_intent(question: str, sql: str, schema: Dict[str, Any]) -> List[Dict[str, str]]:
    q = (question or "")
    ql = q.lower()
    sl = (sql or "").lower()
    issues: List[Dict[str, str]] = []
    if not sl.strip():
        return issues

    def add(kind: str, msg: str) -> None:
        issues.append({"type": f"intent_{kind}", "message": msg})

    # "at least twice", "at most 5" are thresholds, not rankings; "how many days passed" is a duration, not a row count.
    q_rank = re.sub(r"\btop[\s-]+level\b", "toplevel", re.sub(r"\bat (?:least|most)\b", " ", q, flags=re.I), flags=re.I)
    ql_rank = q_rank.lower()
    is_duration = bool(re.search(r"\bhow many\s+(?:whole\s+|full\s+|calendar\s+)?(?:days|weeks|months|years|hours|minutes|seconds)\b", ql))

    # --- aggregation -------------------------------------------------------
    if _AVG_WORDS.search(q) and not re.search(r"\bavg\s*\(|/", sl):
        add("aggregate", "The question asks for an average but the SQL has no AVG() or division.")
    if _SUM_WORDS.search(q) and not re.search(r"\b(sum|count)\s*\(", sl) and not _AVG_WORDS.search(q):
        add("aggregate", "The question asks for a total but the SQL has no SUM()/COUNT().")
    if _COUNT_WORDS.search(q) and not is_duration:
        measure_sum = re.search(r"\b(units?|quantity|quantities|qty|items?|pieces)\b", ql) and re.search(r"\bsum\s*\(", sl)
        if not re.search(r"\bcount\s*\(", sl) and not measure_sum and not _conditional_count(sql) \
                and not _counts_a_measure(ql, sql):
            add("aggregate", "The question asks how many but the SQL has no COUNT().")

    # "top 5 genres BY REVENUE" answered with COUNT(*) only: the ranking is by an amount, not by a number of rows
    by_noun = re.search(r"\b(?:top|bottom|highest|lowest|best|worst|rank(?:ed|ing)?)\b[^?.]*?\bby\s+(?:total\s+)?([a-z_]+)", ql_rank)
    if by_noun:
        from .query_planner import _stem, _tokens
        noun = _stem(by_noun.group(1))
        table_words = {_stem(w) for tn in schema.get("tables", {}) for w in _tokens(tn)}
        count_like = {"number", "count", "frequency", "occurrence", "time", "row", "record"}
        sql_aggs = set(re.findall(r"\b(count|sum|avg|min|max|total)\s*\(", sl))
        if noun not in count_like and noun not in table_words and sql_aggs == {"count"} and not _conditional_count(sql):
            add("aggregate", f"The question ranks by {by_noun.group(1)!r}, an amount, but the SQL only counts rows.")

    # --- ranking -----------------------------------------------------------
    has_window_rank = bool(re.search(r"\b(rank|dense_rank|row_number)\s*\(", sl))
    scalar_extreme = bool(re.search(r"\b(max|min)\s*\(", sl)) and "group by" not in sl and not _TOPN_RE.search(q)
    if _RANK_WORDS.search(q_rank) and "order by" not in sl and not has_window_rank and not scalar_extreme:
        add("ranking", "The question asks for a ranking (top/highest/lowest/...) but the SQL has no ORDER BY.")
    n = _TOPN_RE.search(q)
    if n and "limit" not in sl and not has_window_rank:
        add("ranking", f"The question asks for the {n.group(0).lower()} but the SQL has no LIMIT {n.group(1)}.")
    if n and re.search(r"limit\s+(\d+)", sl):
        lim = int(re.search(r"limit\s+(\d+)", sl).group(1))
        if lim != int(n.group(1)) and not re.search(r"\bgroup by\b.*\border by\b.*\blimit", sl, re.S):
            add("ranking", f"The question asks for {n.group(1)} rows but the SQL has LIMIT {lim}.")
    first_dir = _first_order_direction(sql)   # only the primary sort key decides; "DESC, name ASC" breaks ties
    if re.search(r"\b(top|highest|best|most|largest|biggest|leading|number (?:one|1))\b", ql_rank) and first_dir == "asc" \
            and not re.search(r"\b(lowest|least|fewest|smallest|bottom|worst)\b", ql_rank):
        add("ranking", "The question asks for the highest values but ORDER BY is ascending.")
    if re.search(r"\b(lowest|least|fewest|smallest|bottom|worst)\b", ql_rank) and first_dir == "desc" \
            and not re.search(r"\b(top|highest|best|most|largest|biggest)\b", ql_rank):
        add("ranking", "The question asks for the lowest values but ORDER BY is descending.")

    # --- time filters --------------------------------------------------------
    for y in set(_YEAR_RE.findall(q)):
        if y not in sl:
            add("filter", f"The question mentions {y} but the SQL has no filter on that year.")
    for d in set(_ISO_RE.findall(q)):
        if d not in sl:
            add("filter", f"The question mentions the date {d} but the SQL does not use it.")

    # --- categorical value filters ------------------------------------------
    grouped_by = [re.split(r"\b(?:for|in|from|where|with|among|to|of|and|who|that|which)\b", g)[0]
                  for g in re.findall(r"\b(?:by|per|for each|across|each)\s+([a-z_ ]+)", ql)]
    for tname, tinfo in schema.get("tables", {}).items():
        for cname, pr in (tinfo.get("column_profile") or {}).items():
            if pr.get("kind") != "categorical":
                continue
            for v in pr.get("values", []):
                if len(v) < 3:
                    continue
                if len(v.strip()) <= 3:
                    # short codes ('ALL', 'War', 'EUR') only count when written as stored, not as an everyday word
                    mentioned = re.search(r"(?<![A-Za-z0-9])" + re.escape(v.strip()) + r"(?![A-Za-z0-9])", q)
                else:
                    mentioned = re.search(r"(?<![a-z0-9])" + re.escape(v.lower()) + r"(?![a-z0-9])", ql)
                if mentioned:
                    # ignore when the value is a column-name word in a group-by phrase
                    if v.lower() == cname.lower() or any(v.lower() in g for g in grouped_by):
                        continue
                    if v.lower() not in sl.replace("''", "'"):  # SQL escapes ' as ''
                        add("filter", f"The question mentions {v!r} (a value of {tname}.{cname}) but the SQL does not filter on it.")

    # --- percentage scale ----------------------------------------------------
    pcts = [float(x) for x in _PCT_RE.findall(q)]
    if pcts:
        lits = _sql_literals(sql)
        for cname, pr in _profiles_used(sql, schema):
            scale = pr.get("scale")
            for p in pcts:
                if scale == "fraction_0_1" and abs(p) > 1 and round(p, 6) in lits and round(p / 100, 6) not in lits:
                    add("percent_scale", f"Column {cname} stores fractions (0-1): {p:g}% must be compared as {p / 100:g}, not {p:g}.")
                if scale == "percent_0_100" and abs(p) > 0 and round(p / 100, 6) in lits and round(p, 6) not in lits:
                    add("percent_scale", f"Column {cname} stores percentage points (0-100): {p:g}% must be compared as {p:g}, not {p / 100:g}.")

    # --- grouping --------------------------------------------------------------
    if re.search(r"\b(by|per|for each|each)\s+[a-z]", ql) and (
            _AVG_WORDS.search(q) or _SUM_WORDS.search(q) or _COUNT_WORDS.search(q)) \
            and "group by" not in sl and "over (" not in sl and not re.search(r"\bpartition by\b", sl):
        add("grouping", "The question asks for a breakdown (by/per/each) but the SQL has no GROUP BY.")
    # "as a percentage" answered with a bare ratio (0.417 instead of 41.7)
    if re.search(r"\b(?:percent(?:age)?|porcentaje|pourcentage|prozent|percentuale|percentagem)\b|%|प्रतिशत", q, re.I) \
            and "/" in re.sub(r"'[^']*'", "", sql or "") and not re.search(r"(?<![\w.])100(?:\.0+)?(?![\w.])", sql or ""):
        add("percent_output", "The question asks for a percentage, but the SQL returns a ratio: multiply it by 100.")
    for col, pct in _null_dropping_inequalities(sql, schema):
        add("null_exclusion", f"{col} <> ... (or NOT IN) also drops the rows where {col} is NULL ({pct:.0f}% of the table); "
                              f"use ({col} IS NULL OR {col} <> ...) to keep them.")
    for col in _ungrouped_columns(sql, schema):
        add("ungrouped", f"{col} is selected but neither grouped nor aggregated, so each group shows the value of an "
                         f"arbitrary row; aggregate it (MAX, MIN, SUM...) or add it to GROUP BY.")
    return issues
