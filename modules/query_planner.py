"""Deterministic semantic query plan.

Before any SQL exists, the question is decomposed into an explicit plan: measure, aggregation,
grouping, filters (with exact grounded values), date range, ordering and limit. The plan is
(1) shown to the SQL generator as a contract, (2) used to check the generated SQL's structure
(`check_sql_against_plan`) and (3) when the plan is simple and unambiguous, used to build an
INDEPENDENT reference query (`reference_sql`) whose result is compared with the generated SQL's
result. It never contains dataset-specific constants: every name/value comes from the schema or
the real data.
"""
from __future__ import annotations

import calendar
import re
from typing import Any, Dict, List, Optional, Tuple

import sqlglot
from sqlglot import exp

from .sqlite_compile_check import quote_ident

_NUM = r"(-?\d+(?:\.\d+)?)"
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_STOP = {"the", "a", "an", "of", "for", "by", "per", "in", "on", "to", "and", "or", "is", "what", "which", "show", "me",
         "total", "average", "avg", "mean", "sum", "number", "count", "how", "many", "much", "each", "all", "with", "from"}


def _tokens(s: str) -> List[str]:
    # split CamelCase (ShippedDate, UnitPrice) before lower-casing so column names match question words
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", str(s))
    return [t for t in re.split(r"[^a-z0-9]+", s.lower()) if t]


def _stem(t: str) -> str:
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    if t.endswith("ped") and len(t) > 5:          # shipped -> ship
        return t[:-3]
    if t.endswith("ed") and len(t) > 4 and not t.endswith("eed"):  # ordered -> order, invoiced -> invoic
        return t[:-2]
    if t.endswith("s") and not t.endswith("ss") and len(t) > 3:
        return t[:-1]
    return t


def _rank_text(ql: str) -> str:
    """Lower-cased question with words that look like ranking words but are not removed:
    "at least"/"at most" are thresholds and "top-level" is a hierarchy level, not "top N"."""
    ql = re.sub(r"\bat (?:least|most)\b", " ", ql)
    return re.sub(r"\btop[\s-]+level\b", "toplevel", ql)


_NUM_SCALE = {"thousand": 1e3, "k": 1e3, "million": 1e6, "mn": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9}
# "how many" in other languages: only used to recognise a counting question, never to build a filter
_COUNT_WORDS_INTL = r"(?:\bcu[aá]nt[oa]s\b|\bcombien\b|\bwie ?viele\b|\bquant[oa]s\b|\bquant[ie]\b|\bhoeveel\b|कितन[ेी])"
_FREQ_WORDS = r"(?:appears?|appearing|occurs?|occurring|frequent(?:ly)?|common(?:ly)?|often|popular|repeated)"


def _columns(schema: dict) -> List[Dict[str, Any]]:
    out = []
    for tname, tinfo in (schema.get("tables") or {}).items():
        prof = tinfo.get("column_profile") or {}
        for c in tinfo.get("columns", []):
            out.append({"table": tname, "column": c["name"], "type": str(c.get("type", "")).upper(),
                        "pk": bool(c.get("pk")), "profile": prof.get(c["name"]) or {}})
    return out


def _is_numeric(c) -> bool:
    return any(k in c["type"] for k in ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC")) and c["profile"].get("kind") == "numeric"


def _name_score(qtokens: set, col: str) -> float:
    ct = [_stem(t) for t in _tokens(col)]
    core = [t for t in ct if t not in ("pct", "percent", "percentage", "amt", "amount", "total", "value")]
    ct = core or ct  # unit/generic suffixes (margin_pct, revenue_total) must not prevent a match
    if not ct:
        return 0.0
    hit = sum(1 for t in ct if t in qtokens)
    full = 1.0 if all(t in qtokens for t in ct) else 0.0
    return hit / len(ct) + full


def _pick(qtokens: set, cands: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    scored = sorted(((_name_score(qtokens, c["column"]), c) for c in cands), key=lambda x: -x[0])
    if not scored or scored[0][0] < 1.0:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0] and (
            scored[1][1]["column"] != scored[0][1]["column"] or scored[1][1]["table"] != scored[0][1]["table"]):
        return None  # ambiguous (also Customer.Country vs Supplier.Country) -> do not guess
    return scored[0][1]


def _date_columns(cols):
    return [c for c in cols if c["profile"].get("kind") == "date_text"]


_DATE_WORDS = {"date", "time", "dt", "day", "at", "on", "timestamp"}


def _pick_date_column(q: str, dcols, schema: Optional[dict] = None):
    """The date column the question is about, or None when that is not clear (never "the first date column")."""
    if len(dcols) == 1:
        return dcols[0]
    qt = {_stem(t) for t in _tokens(q)} - _STOP

    def own_words(c):  # ShippedDate -> {ship}; OrderDate in table Order -> {} (the table word says nothing)
        return {_stem(t) for t in _tokens(c["column"])} - _DATE_WORDS - {_stem(t) for t in _tokens(c["table"])}
    specific = [c for c in dcols if own_words(c) & qt]
    if len(specific) == 1:
        return specific[0]
    named = [c for c in dcols if ({_stem(t) for t in _tokens(c["column"])} - _DATE_WORDS) & qt]
    if len(named) == 1:
        return named[0]
    pool = named or dcols
    # the table the question talks about ("orders shipped", "invoices in 2012")
    ent_tables = {c["table"] for c in pool if {_stem(t) for t in _tokens(c["table"])} & qt}
    if len(ent_tables) == 1:
        in_table = [c for c in pool if c["table"] in ent_tables]
        if len(in_table) == 1:
            return in_table[0]
        own = [c for c in in_table if {_stem(t) for t in _tokens(c["table"])} & {_stem(t) for t in _tokens(c["column"])}]
        if len(own) == 1:                      # Order.OrderDate, invoices.InvoiceDate
            return own[0]
    if len({c["table"] for c in pool}) == 1 and len(pool) == 1:
        return pool[0]
    return None


def _date_range(q: str, cols) -> Optional[Dict[str, Any]]:
    dcols = _date_columns(cols)
    if not dcols:
        return None
    dc = _pick_date_column(q, dcols)
    if dc is None:
        return None
    ql = q.lower()
    iso = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", q)
    if len(iso) >= 2:
        return {"table": dc["table"], "column": dc["column"], "start": min(iso[:2]), "end": max(iso[:2]), "inclusive": True}
    m = re.search(r"\bq([1-4])\s*(?:of\s*)?((?:19|20)\d{2})\b", ql) or re.search(r"\b((?:19|20)\d{2})\s*q([1-4])\b", ql)
    if m:
        a, b = m.groups()
        qn, yr = (int(a), int(b)) if len(a) == 1 else (int(b), int(a))
        ms, me = 3 * (qn - 1) + 1, 3 * qn
        return {"table": dc["table"], "column": dc["column"], "start": f"{yr}-{ms:02d}-01",
                "end": f"{yr}-{me:02d}-{calendar.monthrange(yr, me)[1]:02d}", "inclusive": True}
    m = re.search(r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s+((?:19|20)\d{2})\b", ql)
    if m:
        mo, yr = _MONTHS[m.group(1)], int(m.group(2))
        return {"table": dc["table"], "column": dc["column"], "start": f"{yr}-{mo:02d}-01",
                "end": f"{yr}-{mo:02d}-{calendar.monthrange(yr, mo)[1]:02d}", "inclusive": True}
    m = re.search(r"\b(?:in|during|for|of|year)\s+((?:19|20)\d{2})\b", ql)
    if m:
        yr = int(m.group(1))
        return {"table": dc["table"], "column": dc["column"], "start": f"{yr}-01-01", "end": f"{yr}-12-31", "inclusive": True}
    return None


_SINGULAR_S = ("ss", "us", "is", "sis", "ics")


def _asks_for_plural(ql: str) -> bool:
    """"Which industries had the highest sales" asks for several rows; "which industry" for one."""
    m = re.search(r"\b(?:which|what|who)\s+(?:are\s+the\s+|were\s+the\s+)?(?:[a-z-]+\s+){0,2}?([a-z]+)\s+(?:had|have|has|are|were|did|do|is|was|sold|made|generated|with|having)\b", ql)
    word = m.group(1) if m else ""
    return bool(word) and word.endswith("s") and not word.endswith(_SINGULAR_S)


def build_plan(question: str, schema: dict, grounded: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    q = question or ""
    ql = q.lower()
    # "at least"/"at most" are threshold words and "top-level" is a hierarchy level, not ranking words (the original
    # text is kept in `ql` for the threshold parser below).
    ql_rank = _rank_text(ql)
    is_duration = bool(re.search(r"\bhow many\s+(?:whole\s+|full\s+|calendar\s+)?(?:days|weeks|months|years|hours|minutes|seconds)\b", ql))
    cols = _columns(schema)
    qt = {_stem(t) for t in _tokens(q)}
    plan: Dict[str, Any] = {"tables": [], "measure": None, "aggregation": None, "group_by": [], "filters": [],
                            "date_range": None, "order": None, "limit": None, "notes": [], "verifiable": False}

    # --- filters from grounded categorical values (exact stored spelling) ---------------------------------
    by_col: Dict[Tuple[str, str], List[str]] = {}
    for h in grounded or []:
        if h.get("match") == "exact":
            by_col.setdefault((h["table"], h["column"]), []).append(h["value"])
    # The same mention found in several columns (Customer.Country and Invoice.BillingCountry both store 'Germany'):
    # filtering on any one of them answers the question, so the plan names alternatives instead of guessing one.
    by_vals: Dict[Tuple[str, ...], List[Tuple[str, str]]] = {}
    for (t, c), vals in by_col.items():
        by_vals.setdefault(tuple(sorted(vals)), []).append((t, c))
    for vals, places in by_vals.items():
        t, c = places[0]
        f = {"table": t, "column": c, "op": "=" if len(vals) == 1 else "in",
             "value": vals[0] if len(vals) == 1 else list(vals), "source": "grounded"}
        if len(places) > 1:
            f["alternatives"] = [{"table": tt, "column": cc} for tt, cc in places]
        plan["filters"].append(f)

    # --- aggregation -----------------------------------------------------------------------------------------
    if re.search(r"\b(median|percentile|quartile|mode)\b", ql):
        plan["aggregation"] = "median"       # no SQLite aggregate: only described to the generator, never enforced
        plan["notes"].append("median/percentile is not modelled by the reference calculation")
    elif re.search(r"\b(distinct|unique)\b", ql) and re.search(r"\b(how many|number of|count)\b", ql):
        plan["aggregation"] = "count_distinct"
    elif re.search(r"\b(average|avg|mean)\b", ql):
        plan["aggregation"] = "avg"
    elif re.search(r"\b(how many|number of|counts?)\b", ql) and not is_duration and not re.search(r"\b(units?|quantity|qty|items?)\b", ql):
        plan["aggregation"] = "count"
    elif re.search(_COUNT_WORDS_INTL, ql):
        plan["aggregation"] = "count"         # "¿Cuántas tareas hay en total?" counts rows despite the word "total"
    elif re.search(r"\b(maximum|max|largest single|biggest single|highest single)\b", ql):
        plan["aggregation"] = "max"   # an explicit extreme word beats the generic "overall"/"largest" wording
    elif re.search(r"\b(minimum|min|smallest single|lowest single)\b", ql):
        plan["aggregation"] = "min"
    elif re.search(r"\b(total(?:s|led|ed|ling|ing)?|sum(?:med)?|summed|combined|overall)\b", ql):
        plan["aggregation"] = "sum"
    elif re.search(r"\b(most|least|fewest)\s+(?:\w+\s+){0,1}?" + _FREQ_WORDS + r"\b|\b" + _FREQ_WORDS + r"\s+(?:the\s+)?(most|least)\b", ql_rank):
        # "which project appears most", "the most common currency": a frequency question, i.e. COUNT per entity
        plan["aggregation"] = "count"
        plan["notes"].append("frequency question")
    elif re.search(r"\b(highest|lowest|top|best|worst|most|least|fewest|largest|smallest|biggest|rank)\b", ql_rank) \
            and re.search(r"\b(by|per|each|which)\b", ql):
        plan["aggregation"] = "sum"
        plan["notes"].append("aggregation inferred from a ranking question")

    # --- measure ---------------------------------------------------------------------------------------------
    num_cols = [c for c in cols if _is_numeric(c) and not c["pk"] and not re.search(r"(^|_)(id|year|month|rank)($|_)", c["column"].lower())]
    table_stems = {tname: {_stem(t) for t in _tokens(tname)} for tname in (schema.get("tables") or {})}
    # "the most customers"/"the fewest orders": the ranked thing is an entity (a table), so the aggregate is COUNT.
    most_noun = re.search(r"\b(?:most|fewest|least|highest number of|largest number of|greatest number of)\s+([a-z_]+)", ql_rank)
    if most_noun and "aggregation inferred from a ranking question" in plan["notes"]:
        noun = _stem(most_noun.group(1))
        if any(noun in st for st in table_stems.values()) and not _pick({noun}, num_cols):
            plan["aggregation"] = "count"
            plan["notes"].remove("aggregation inferred from a ranking question")
            plan["notes"].append("count of entities inferred from a ranking question")
    # "how many days are required in total", "total number of units": the counted noun is a numeric measure -> SUM of it
    if plan["aggregation"] == "count":
        cm = re.search(r"\b(?:how many|number of)\s+([a-z_ ]{3,40})", ql)
        if cm:
            words = [_stem(w) for w in _tokens(cm.group(1))][:3]
            # the counted noun itself must be the measure ("number of DAYS required"), not a later word
            # ("how many ROWS have no data value", "how many COUNTRIES are eu members")
            m = _pick(set(words) - _STOP, [c for c in num_cols if words and words[0] in {_stem(x) for x in _tokens(c["column"])}])
            if m:
                plan["aggregation"] = "sum"
                plan["measure"] = {"table": m["table"], "column": m["column"]}
                plan["tables"] = [m["table"]]
                plan["notes"].append("counted noun is a numeric measure")
    if (plan["aggregation"] and plan["aggregation"] != "count" and not plan["measure"]) or \
            (not plan["aggregation"] and re.search(r"\b(top|bottom|highest|lowest|largest|smallest|biggest)\b", ql_rank)):
        m = _pick(qt, num_cols)
        if m:
            plan["measure"] = {"table": m["table"], "column": m["column"]}
            plan["tables"] = [m["table"]]
    # --- group by --------------------------------------------------------------------------------------------
    group_cands = [c for c in cols if c["profile"].get("kind") == "categorical" or ("TEXT" in c["type"] and not c["profile"])]
    gb_phrases = re.findall(r"\b(?:by|per|for each|each|across|which)\s+([a-z_0-9 ]+?)(?=\s+(?:for|in|from|where|with|among|had|has|have|did|was|were|is|are|sold|generated|made|appears?|occurs?|shows? up|uses?|used)\b|[?,.]|$)", ql)
    # "the group with the largest ...", "the two plans having the lowest ..." -> the noun before with/having is the entity
    gb_phrases += re.findall(r"\b(?:the|name|show|list|give|top|bottom|\d+|two|three|four|five)\s+([a-z_]+?)s?\s+(?:with|having|that (?:has|have))\s+(?:the\s+)?(?:highest|lowest|largest|smallest|most|least|fewest|biggest|best|worst)\b", ql)
    for phrase in gb_phrases:
        gt = {_stem(t) for t in _tokens(phrase)} - _STOP
        cand = [c for c in group_cands if not plan["tables"] or c["table"] == plan["tables"][0]]
        g = _pick(gt, cand) if gt else None
        if g and g["column"] not in [x["column"] for x in plan["group_by"]] \
                and not any(f["column"] == g["column"] for f in plan["filters"]):
            plan["group_by"].append({"table": g["table"], "column": g["column"]})
            if not plan["tables"]:
                plan["tables"] = [g["table"]]
    if plan["group_by"] and plan["measure"] and plan["group_by"][0]["table"] != plan["measure"]["table"]:
        plan["notes"].append("measure and grouping come from different tables (join required)")
    if "aggregation inferred from a ranking question" in plan["notes"] and not plan["group_by"]:
        plan["aggregation"] = None            # row-level ranking ("top 5 orders by revenue"): no aggregate
        plan["notes"].remove("aggregation inferred from a ranking question")
        plan["notes"].append("row-level ranking")
    # --- date range ------------------------------------------------------------------------------------------
    dr = _date_range(q, cols)
    if dr:
        plan["date_range"] = dr
        if not plan["tables"]:
            plan["tables"] = [dr["table"]]
    # --- numeric thresholds ---------------------------------------------------------------------------------
    unmodelled_threshold = False
    for m in re.finditer(r"\b(above|over|greater than|more than|exceeds?|exceeding|higher than|below|under|less than|fewer than|lower than|at least|at most)\s+(?:a\s+)?" + _NUM
                         + r"\s*(%|percent|per cent|thousand|million|billion|mn|bn|k|m|b)?\b", ql):
        word, val, unit = m.group(1), float(m.group(2)), m.group(3)
        pct = unit if unit in ("%", "percent", "per cent") else None
        if unit in _NUM_SCALE:
            val *= _NUM_SCALE[unit]        # "over 50 million" is 50,000,000, not 50
        prev = ql[max(0, m.start() - 60):m.start()]
        # only the clause the threshold belongs to: "mean units WHERE units exceeds 10" is a row filter, while
        # "projects that HAVE an average progress below 20%" thresholds the aggregate
        clause = re.split(r"\b(?:where|with|whose|for|that|which|who|having|have|has|and|in)\b", prev)[-1]
        if re.search(r"\b(average|avg|mean|median|total|sum)\b", clause):
            # "projects whose AVERAGE progress is below 20%": a HAVING on an aggregate, not a row filter
            unmodelled_threshold = True
            continue
        before = {_stem(t) for t in _tokens(prev)} - _STOP
        after_m = re.match(r"\s*(?:of\s+)?(?:a\s+)?([a-z_ ]{3,30})", ql[m.end():])
        cand_tokens = before | ({_stem(t) for t in _tokens(after_m.group(1))} - _STOP if (pct and after_m) else set())
        col = _pick(cand_tokens, [c for c in num_cols])
        if not col:
            # e.g. "tiers with more than 120 customers" is a HAVING on the count, which the reference query cannot model
            unmodelled_threshold = True
            continue
        op = {"above": ">", "over": ">", "greater than": ">", "more than": ">", "exceeds": ">", "exceed": ">", "exceeding": ">",
              "higher than": ">", "below": "<", "under": "<", "less than": "<", "fewer than": "<", "lower than": "<",
              "at least": ">=", "at most": "<="}[word]
        v = val
        if pct and col["profile"].get("scale") == "fraction_0_1":
            v = round(val / 100.0, 10)
        plan["filters"].append({"table": col["table"], "column": col["column"], "op": op, "value": v, "source": "threshold"})
        if not plan["tables"]:
            plan["tables"] = [col["table"]]
    # --- ordering / limit -----------------------------------------------------------------------------------
    n = re.search(r"\b(top|bottom|first|last)\s+(\d+)\b", ql) or re.search(r"\b(?:the\s+)?(\d+)\s+(?:highest|lowest|best|worst|largest|smallest|biggest|top|bottom|most|least)\b", ql)
    desc_w = re.search(r"\b(top|highest|best|most|largest|biggest|leading|number (?:one|1))\b", ql_rank)
    asc_w = re.search(r"\b(bottom|lowest|worst|least|fewest|smallest)\b", ql_rank)
    scalar_extreme = plan["aggregation"] in ("max", "min") and not plan["group_by"] and not n
    if (desc_w or asc_w) and not scalar_extreme:  # MAX()/MIN() over all rows already answers "largest"/"smallest"
        plan["order"] = {"direction": "asc" if (asc_w and not desc_w) else "desc", "by": "measure"}
        if n:
            plan["limit"] = int(n.group(2) if n.group(1) in ("top", "bottom", "first", "last") and n.lastindex and n.lastindex >= 2 else n.group(1))
        else:
            ints = [int(x) for x in re.findall(r"(?<![\d.\-/])(\d{1,3})(?![\d.%\-/]|\s*(?:%|percent))", ql)]
            if len(ints) == 1 and 1 <= ints[0] <= 500:
                plan["limit"] = ints[0]
            elif re.search(r"\b(two|three|four|five|six|seven|eight|nine|ten)\b", ql):
                plan["limit"] = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}[
                    re.search(r"\b(two|three|four|five|six|seven|eight|nine|ten)\b", ql).group(1)]
            elif re.search(r"\bwhich\b|\bwho\b|\bnumber (?:one|1)\b|\b(?:the|name|show)\s+\w+\s+(?:with|having)\s+the\b", ql) \
                    and not _asks_for_plural(ql):
                plan["limit"] = 1
    if not plan["tables"] and plan["filters"] and len({f["table"] for f in plan["filters"]}) == 1:
        plan["tables"] = [plan["filters"][0]["table"]]
    # --- long-format statistics tables: one hierarchy level and one version (see modules/long_format.py) ----------
    lf_table = plan["tables"][0] if plan["tables"] else None
    lf_info = ((schema.get("tables") or {}).get(lf_table) or {}).get("long_format") if lf_table else None
    lf_unfixed_qualifier = False
    if lf_info and plan["aggregation"] not in ("count", "count_distinct"):
        if plan["aggregation"] in ("sum", "avg", "min", "max", "median") and not plan["measure"] and len(lf_info["measures"]) == 1:
            plan["measure"] = {"table": lf_table, "column": lf_info["measure"]}
        reads_values = bool(plan["measure"] and plan["measure"]["column"] == lf_info["measure"]) or \
            any(f["column"] == lf_info["entity"] for f in plan["filters"])
        if reads_values:
            from .long_format import required
            fixed = {f["column"] for f in plan["filters"]} | {g["column"] for g in plan["group_by"]}
            for req in required(question, schema, lf_table, grounded):
                if req["kind"] == "qualifier" and len(req["choices"]) > 1:
                    continue                  # two variables named (a margin): the SQL combines them, no single filter
                if req["column"] not in fixed:
                    plan["filters"].append({"table": lf_table, "column": req["column"], "op": "=", "value": req["value"],
                                            "source": "structure", "choices": req["choices"]})
                    plan["notes"].append(f"{req['column']} fixed to one {req['kind'].replace('_', ' ')} ({req['from']})")
            # a variable column the question does not pin down would make a reference total add different variables
            fixed |= {f["column"] for f in plan["filters"]}
            lf_unfixed_qualifier = any(c["kind"] == "qualifier" and c["column"] not in fixed for c in lf_info["columns"])
    # --- counted entity ---------------------------------------------------------------------------------------
    # "How many ORDERS per store region?" counts rows of `orders`, not of the table that owns `region`. Without this
    # the reference query counted stores per region and flagged correct join queries as a mismatch.
    entity_table = None
    if plan["aggregation"] in ("count", "count_distinct"):
        em = re.search(r"\b(?:how many|number of|count of|count)\s+(?:distinct\s+|unique\s+|different\s+)?([a-z_]+)", ql)
        if not em and "count of entities inferred from a ranking question" in plan["notes"]:
            em = most_noun                         # "which region placed the most ORDERS" counts orders, not regions
        if em:
            ent = _stem(em.group(1))
            for tname in (schema.get("tables") or {}):
                tt = [_stem(x) for x in _tokens(tname)]
                if tt and (tt[-1] == ent or "_".join(tt) == ent):
                    entity_table = tname
                    break
    if entity_table and plan["tables"]:
        if entity_table != plan["tables"][0]:
            plan["notes"].append(f"counts rows of {entity_table} but groups/filters on {plan['tables'][0]} (join required)")
    # --- verifiability --------------------------------------------------------------------------------------
    single_table = len(set(plan["tables"] or [])) == 1 and all(
        (f["table"] == plan["tables"][0]) for f in plan["filters"]) and all(
        g["table"] == plan["tables"][0] for g in plan["group_by"])
    has_agg_target = plan["aggregation"] == "count" or plan["aggregation"] == "count_distinct" or plan["measure"]
    ranking_wo_group = bool(plan["order"]) and not plan["group_by"]  # a ranking of unknown entities cannot be checked independently
    no_open_by = not re.search(r"\b(by|per|for each)\b", ql) or bool(plan["group_by"])
    # "each month of 2013", "monthly revenue", "per year": time buckets the reference query does not model
    ql_words = ql
    for h in grounded or []:                       # a stored value ('Premium Tier (Annual)') is data, not wording
        ql_words = ql_words.replace(str(h.get("value", "")).lower(), " ")
    time_buckets = bool(re.search(r"\b(?:each|every|per|by|for each)\s+(?:calendar\s+)?(?:day|week|month|quarter|year)s?\b"
                                  r"|\b(?:daily|weekly|monthly|quarterly|yearly|annual(?:ly)?|over time|trend)\b", ql_words))
    entity_elsewhere = bool(entity_table and plan["tables"] and entity_table != plan["tables"][0])
    # The question names a date/period the planner could not turn into a date_range (e.g. the date column was not
    # profiled as text dates): a reference query without that filter would be wrong, so do not build one.
    mentions_period = bool(re.search(r"\b(?:19|20)\d{2}\b|\b\d{4}-\d{2}-\d{2}\b|\bq[1-4]\b|\b(?:"
                                     + "|".join(sorted((m for m in _MONTHS if len(m) > 3), key=len, reverse=True))
                                     + r")\b", ql))
    unmodelled_period = mentions_period and not plan["date_range"]
    ok = bool(plan["aggregation"] and single_table and has_agg_target and no_open_by and not ranking_wo_group
              and plan["aggregation"] != "count_distinct" and not entity_elsewhere and not unmodelled_threshold
              and not unmodelled_period and not any(f.get("alternatives") for f in plan["filters"]) and not time_buckets
              and plan["aggregation"] != "median" and not lf_unfixed_qualifier
              and not any(len(f.get("choices") or []) > 1 for f in plan["filters"]))
    plan["verifiable"] = ok
    if not ok:
        plan["notes"].append("plan is not unambiguous enough for an independent reference calculation")
    return plan


# ---------------------------------------------------------------------------------------------------------------
def plan_to_prompt_text(plan: Dict[str, Any]) -> str:
    from .value_grounding import sanitize_for_prompt
    lines = ["QUERY PLAN (derived from the question; the SQL must implement exactly this):"]
    if plan.get("aggregation"):
        m = plan.get("measure")
        lines.append(f" - aggregation: {plan['aggregation'].upper()}" + (f" of {m['table']}.{m['column']}" if m else ""))
    if plan.get("group_by"):
        lines.append(" - group by: " + ", ".join(f"{g['table']}.{g['column']}" for g in plan["group_by"]))
    for f in plan.get("filters", []):
        v = sanitize_for_prompt(f["value"]) if isinstance(f["value"], str) else (
            "(" + ", ".join(sanitize_for_prompt(x) for x in f["value"]) + ")" if isinstance(f["value"], list) else f["value"])
        lines.append(f" - filter: {f['table']}.{f['column']} {f['op']} {v}")
    dr = plan.get("date_range")
    if dr:
        lines.append(f" - date range on {dr['table']}.{dr['column']}: {dr['start']} to {dr['end']} (inclusive both ends)")
    if plan.get("order"):
        lines.append(f" - order by the measure {plan['order']['direction'].upper()}" + (f", LIMIT {plan['limit']}" if plan.get("limit") else ""))
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def reference_sql(plan: Dict[str, Any]) -> Optional[Tuple[str, List[Any]]]:
    """Independent parameterised reference query for a verifiable plan (None when not verifiable)."""
    if not plan.get("verifiable"):
        return None
    t = quote_ident(plan["tables"][0])
    agg = plan["aggregation"]
    m = plan.get("measure")
    expr = {"sum": "SUM({c})", "avg": "AVG({c})", "min": "MIN({c})", "max": "MAX({c})"}.get(agg)
    if agg == "count":
        expr = "COUNT(*)"
    elif expr and m:
        expr = expr.format(c=quote_ident(m["column"]))
    else:
        return None
    gb = [quote_ident(g["column"]) for g in plan.get("group_by", [])]
    where, params = [], []
    for f in plan.get("filters", []):
        if f["op"] == "in":
            where.append(f"{quote_ident(f['column'])} IN ({', '.join('?' for _ in f['value'])})")
            params.extend(f["value"])
        else:
            where.append(f"{quote_ident(f['column'])} {f['op']} ?")
            params.append(f["value"])
    dr = plan.get("date_range")
    if dr:
        where.append(f"{quote_ident(dr['column'])} >= ? AND {quote_ident(dr['column'])} < date(?, '+1 day')")
        params += [dr["start"], dr["end"]]
    sql = f"SELECT {', '.join(gb + [expr + ' AS value'])} FROM {t}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if gb:
        sql += " GROUP BY " + ", ".join(gb)
    if plan.get("order"):
        sql += f" ORDER BY value {plan['order']['direction'].upper()}" + "".join(f", {g}" for g in gb)
    if plan.get("limit"):
        sql += f" LIMIT {int(plan['limit'])}"
    return sql, params


def _has_conditional_count(tree: exp.Expression) -> bool:
    """SUM(CASE WHEN cond THEN 1 ELSE 0 END), SUM(IIF(cond, 1, 0)) and SUM(cond) count rows just like COUNT(...)."""
    for s in tree.find_all(exp.Sum):
        arg = s.this
        if isinstance(arg, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Like, exp.And, exp.Or, exp.Not, exp.Is)):
            return True
        if isinstance(arg, (exp.Case, exp.If)):
            outs = [i.args.get("true") for i in arg.args.get("ifs", [])] + [arg.args.get("default")] if isinstance(arg, exp.Case) \
                else [arg.args.get("true"), arg.args.get("false")]
            vals = {o.this for o in outs if isinstance(o, exp.Literal) and not o.is_string}
            if vals and vals <= {"0", "1"} and "1" in vals:
                return True
    return False


def check_sql_against_plan(sql: str, plan: Dict[str, Any]) -> List[Dict[str, str]]:
    """Structural check of the generated SQL against the plan (AST based, no execution)."""
    issues: List[Dict[str, str]] = []
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return issues
    low = sql.lower()

    def add(kind, msg):
        issues.append({"type": f"plan_{kind}", "message": msg})

    agg_types = {"sum": exp.Sum, "avg": exp.Avg, "min": exp.Min, "max": exp.Max, "count": exp.Count}
    want = plan.get("aggregation")
    # A SUM/AVG/MIN/MAX is only enforced when the plan identified the measure; without one the word ("total", "en
    # total") does not say which aggregate answers the question.
    enforce = want == "count" or (want in agg_types and plan.get("measure"))
    if enforce and tree.find(agg_types[want]) is None and not (want == "avg" and "/" in low) \
            and not (want == "count" and _has_conditional_count(tree)):
        add("aggregation", f"Plan requires {want.upper()} but the SQL does not use it.")
    elif want == "sum" and not plan.get("measure"):
        # "top 5 genres by revenue" answered with COUNT(*): an amount was asked for, rows were counted
        aggs = {type(a) for a in tree.find_all(exp.AggFunc)}
        if aggs and aggs <= {exp.Count} and not _has_conditional_count(tree):
            add("aggregation", "The question asks for a total or ranking of an amount, but the SQL only counts rows.")
    for g in plan.get("group_by", []):
        grp = tree.find(exp.Group)
        names = {c.name.lower() for c in grp.find_all(exp.Column)} if grp else set()
        if g["column"].lower() not in names and "over (" not in low:
            add("grouping", f"Plan requires grouping by {g['column']}.")
    cmp_nodes = list(tree.find_all(exp.EQ)) + list(tree.find_all(exp.GT)) + list(tree.find_all(exp.GTE)) + \
        list(tree.find_all(exp.LT)) + list(tree.find_all(exp.LTE)) + list(tree.find_all(exp.In)) + list(tree.find_all(exp.Like))
    for f in plan.get("filters", []):
        if f.get("source") == "structure":
            continue                       # hierarchy level / version: checked by long_format.check_sql (aliases, GROUP BY)
        cols = {f["column"].lower()} | {a["column"].lower() for a in f.get("alternatives", [])}
        # every comparison on the column counts: conditional aggregation compares one column with several values
        nodes = [n for n in cmp_nodes if any(c.name.lower() in cols for c in n.find_all(exp.Column))]
        if not nodes:
            add("filter", f"Plan requires a filter on {f['column']} {f['op']} {f['value']!r} but the SQL has none.")
            continue
        if f["source"] == "grounded" and f["op"] == "=":
            lits = [l.this for n in nodes if not isinstance(n, exp.Like) for l in n.find_all(exp.Literal) if l.is_string]
            # another planned filter on a same-named column (genres.Name vs tracks.Name) may own that literal
            other_vals = {str(o["value"]) for o in plan.get("filters", []) if o is not f and o["column"].lower() in cols}
            lits = [l for l in lits if l not in other_vals]
            if lits and f["value"] not in lits and not any(isinstance(n, exp.Like) for n in nodes):
                add("exact_value", f"Filter on {f['column']} uses {lits[0]!r}; the stored value is {f['value']!r}.")
        if f["source"] == "threshold":
            nums = [float(l.this) for n in nodes for l in n.find_all(exp.Literal) if not l.is_string]
            if nums and not any(abs(x - float(f["value"])) < 1e-9 for x in nums):
                add("filter_value", f"Filter on {f['column']} compares with {nums[0]:g}; the question implies {f['value']:g} (check percent scale).")
    dr = plan.get("date_range")
    if dr:
        if dr["column"].lower() not in low or not (dr["start"] in sql or dr["end"] in sql or "strftime" in low or "substr" in low):
            add("date_range", f"Plan requires a date range on {dr['column']}: {dr['start']} to {dr['end']}.")
        elif dr["end"] in sql and re.search(r"<\s*'" + re.escape(dr["end"]) + "'", sql):
            add("date_range", f"The range end {dr['end']} must be inclusive (use <= or < next day).")
    o = plan.get("order")
    if o:
        ob = tree.find(exp.Order)
        if ob is None and not re.search(r"\b(rank|row_number)\s*\(", low):
            add("ordering", "Plan requires ORDER BY.")
        elif ob is not None:
            first = ob.expressions[0] if ob.expressions else None
            is_desc = bool(first is not None and first.args.get("desc"))
            if is_desc != (o["direction"] == "desc"):
                add("ordering", f"Plan requires ORDER BY {o['direction'].upper()}.")
    if plan.get("limit"):
        lim = tree.find(exp.Limit)
        if lim is None:
            add("limit", f"Plan requires LIMIT {plan['limit']}.")
        else:
            try:
                if int(lim.expression.name) != int(plan["limit"]):
                    add("limit", f"Plan requires LIMIT {plan['limit']}, SQL has LIMIT {lim.expression.name}.")
            except Exception:
                pass
    return issues
