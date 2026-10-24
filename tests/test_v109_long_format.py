"""Long-format statistics tables (Stats NZ style): hierarchy levels and versions must never be added together.

The fixture is generated here, not taken from the owner's file: level-2 industries roll up exactly into level-1 parents,
one industry exists at both levels with identical rows (like 'Mining'), every figure exists as an unadjusted series and
as seasonally adjusted / trend versions, and a 'suppressed' flag is empty on most rows. Every expected answer is computed
in Python from the generating dictionaries below, never by SQL.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from modules.long_format import check_sql
from modules.query_planner import build_plan, reference_sql
from modules.schema_engine import load_database
from modules.sql_intent_checker import check_intent

T = "stats"
L1, L2 = "Industry (Level 1)", "Industry (Level 2)"
PERIODS = [2024.03, 2024.06, 2024.09, 2024.12, 2025.03, 2025.06, 2025.09, 2025.12]
VARIABLES = ["Sales", "Wages"]
# level-2 children of each level-1 parent; 'Mining' has no breakdown, so it appears at both levels with the same rows
CHILDREN = {"Manufacturing": ["Food Manufacturing", "Metal Manufacturing"],
            "Retail": ["Food Retail", "Clothing Retail", "Fuel Retail"],
            "Mining": ["Mining"]}
SEASONAL = [0.9, 1.1, 0.95, 1.05]          # averages 1 over a year, so adjusted series keep annual totals


def _child_value(child: str, variable: str, period: float) -> float:
    base = 100.0 + 13.0 * sorted({c for cs in CHILDREN.values() for c in cs}).index(child) + (40.0 if variable == "Sales" else 0.0)
    return round(base * (1 + 0.02 * PERIODS.index(period)), 3)


def _rows():
    """(period, grp, basis, variable, industry, adjustment, value, suppressed); basis relabels grp 1:1."""
    rows = []
    for variable in VARIABLES:
        for i, period in enumerate(PERIODS):
            for parent, kids in CHILDREN.items():
                for adj in ("Unadjusted", "Seasonally adjusted", "Trend"):
                    if adj != "Unadjusted" and i < 4:
                        continue                   # adjusted series start later: Unadjusted is the most complete
                    factor = 1.0 if adj == "Unadjusted" else SEASONAL[i % 4] if adj == "Seasonally adjusted" else 1.0
                    total = 0.0
                    for kid in kids:
                        v = round(_child_value(kid, variable, period) * factor, 3)
                        total += v
                        rows.append((period, L2, "Detailed prices", variable, kid, adj, v, None))
                    rows.append((period, L1, "Current prices", variable, parent, adj, round(total, 3), None))
    rows[3] = rows[3][:-1] + ("Y",)            # a few suppressed rows
    rows[10] = rows[10][:-1] + ("Y",)
    return rows


ROWS = _rows()


@pytest.fixture()
def stats(tmp_path):
    p = tmp_path / "stats.db"
    con = sqlite3.connect(p)
    con.execute(f'CREATE TABLE {T}(period REAL, "group" TEXT, basis TEXT, variable TEXT, industry TEXT, adjustment TEXT, '
                'value REAL, suppressed TEXT)')
    con.executemany(f"INSERT INTO {T} VALUES (?,?,?,?,?,?,?,?)", ROWS)
    con.commit()
    con.close()
    return str(p), load_database(str(p))


def _expected(level, variable, adjustment, period=None, industry=None):
    """Independent answer from the Python rows: {industry: total}."""
    out = {}
    for per, grp, _b, var, ind, adj, val, _s in ROWS:
        if grp == level and var == variable and adj == adjustment and (period is None or per == period) \
                and (industry is None or ind == industry):
            out[ind] = round(out.get(ind, 0.0) + val, 3)
    return out


# ------------------------------------------------------------------------------------------------------- detection
def test_detects_hierarchy_levels_versions_and_variables(stats):
    _, schema = stats
    lf = schema["tables"][T]["long_format"]
    kinds = {c["column"]: c for c in lf["columns"]}
    assert lf["measure"] == "value" and lf["entity"] == "industry" and lf["time"] == ["period"]
    assert kinds["group"]["kind"] == "hierarchy_level" and kinds["group"]["levels"] == [L1, L2]   # fewest entities first
    assert kinds["group"]["entities_per_level"] == {L1: 3, L2: 6} and kinds["group"]["entities_in_several_levels"] == 1
    assert kinds["group"]["aliases"] == ["basis"]                  # 'basis' relabels the levels 1:1
    assert kinds["adjustment"]["kind"] == "versions" and kinds["adjustment"]["default"] == "Unadjusted"
    assert kinds["variable"]["kind"] == "qualifier"
    json.dumps(lf)                                                 # stored with the session schema


def test_ordinary_tables_are_not_long_format(tmp_path):
    p = tmp_path / "plain.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE orders(order_id INTEGER PRIMARY KEY, region TEXT, channel TEXT, amount REAL)")
    con.executemany("INSERT INTO orders VALUES (?,?,?,?)",
                    [(i, ["N", "S", "E"][i % 3], ["web", "shop"][i % 2], 10.0 + i) for i in range(60)])
    con.commit()
    con.close()
    assert "long_format" not in load_database(str(p))["tables"]["orders"]


def test_independent_check_of_the_fixture():
    # level 1 and level 2 hold the same totals; that is why adding them doubles every figure
    l1 = sum(_expected(L1, "Sales", "Unadjusted", 2025.12).values())
    l2 = sum(_expected(L2, "Sales", "Unadjusted", 2025.12).values())
    assert abs(l1 - l2) < 0.01 and l1 > 0


# ---------------------------------------------------------------------------------------------- SQL check (repair)
Q_TOP = "Which top-level industry had the highest unadjusted sales in the December 2025 quarter?"


def _issues(schema, path, question, sql):
    from modules.value_grounding import ground_question
    g = ground_question(question, schema, path)
    return [i["type"] for i in check_sql(question, sql, schema, g, path)]


def test_summing_across_levels_or_versions_is_flagged(stats):
    path, schema = stats
    no_level = (f"SELECT industry, SUM(value) AS s FROM {T} WHERE variable = 'Sales' AND adjustment = 'Unadjusted' "
                f"AND period = 2025.12 GROUP BY industry ORDER BY s DESC LIMIT 1")
    assert "long_format_hierarchy_level" in _issues(schema, path, Q_TOP, no_level)
    no_version = (f"SELECT industry, SUM(value) AS s FROM {T} WHERE variable = 'Sales' AND \"group\" = '{L1}' "
                  f"AND period = 2025.12 GROUP BY industry ORDER BY s DESC LIMIT 1")
    assert "long_format_versions" in _issues(schema, path, Q_TOP, no_version)
    by_level = (f"SELECT \"group\", SUM(value) AS s FROM {T} WHERE variable = 'Sales' AND adjustment = 'Unadjusted' "
                f"AND period = 2025.12 GROUP BY \"group\" ORDER BY s DESC LIMIT 1")
    assert "long_format_level_grouping" in _issues(schema, path, Q_TOP, by_level)
    wrong_level = no_level.replace("WHERE ", f"WHERE \"group\" = '{L2}' AND ")
    assert "long_format_hierarchy_level_value" in _issues(schema, path, Q_TOP, wrong_level)


def test_correct_sql_is_not_flagged_and_matches_the_independent_answer(stats):
    path, schema = stats
    ok = (f"SELECT industry, SUM(value) AS s FROM {T} WHERE variable = 'Sales' AND adjustment = 'Unadjusted' "
          f"AND \"group\" = '{L1}' AND period = 2025.12 GROUP BY industry ORDER BY s DESC, industry LIMIT 1")
    assert _issues(schema, path, Q_TOP, ok) == []
    assert _issues(schema, path, Q_TOP, ok.replace(f"\"group\" = '{L1}'", "basis = 'Current prices'")) == []   # alias
    con = sqlite3.connect(path)
    got = con.execute(ok).fetchall()
    con.close()
    exp = _expected(L1, "Sales", "Unadjusted", 2025.12)
    top = max(exp.items(), key=lambda kv: (kv[1], [-ord(ch) for ch in kv[0]]))
    assert got == [(top[0], pytest.approx(top[1], abs=1e-6))]
    # the wrong query (no level) really is wrong: Mining is counted at both levels
    con = sqlite3.connect(path)
    mixed = dict(con.execute(f"SELECT industry, SUM(value) FROM {T} WHERE variable = 'Sales' AND adjustment = 'Unadjusted' "
                             "AND period = 2025.12 GROUP BY industry").fetchall())
    con.close()
    assert mixed["Mining"] == pytest.approx(2 * exp["Mining"])


def test_an_entity_at_one_level_needs_no_level_filter_but_a_shared_one_does(stats):
    path, schema = stats
    q_retail = "Total unadjusted Retail sales in 2025"
    sql = f"SELECT SUM(value) FROM {T} WHERE industry = 'Retail' AND variable = 'Sales' AND adjustment = 'Unadjusted' AND period >= 2025 AND period < 2026"
    assert _issues(schema, path, q_retail, sql) == []
    q_mining = "Total unadjusted Mining sales in 2025"
    assert "long_format_hierarchy_level" in _issues(schema, path, q_mining, sql.replace("Retail", "Mining"))
    con = sqlite3.connect(path)
    with_level = con.execute(sql.replace("Retail", "Mining") + f" AND \"group\" = '{L2}'").fetchone()[0]
    con.close()
    want = sum(v for per, grp, _b, var, ind, adj, v, _s in ROWS
               if ind == "Mining" and grp == L2 and var == "Sales" and adj == "Unadjusted" and 2025 <= per < 2026)
    assert with_level == pytest.approx(want)        # either level gives the same Mining figures


def test_detailed_level_question_requires_level_two(stats):
    path, schema = stats
    q = "Which level 2 industry had the highest unadjusted sales in the December 2025 quarter?"
    l1 = (f"SELECT industry, SUM(value) AS s FROM {T} WHERE \"group\" = '{L1}' AND variable = 'Sales' "
          f"AND adjustment = 'Unadjusted' AND period = 2025.12 GROUP BY industry ORDER BY s DESC LIMIT 1")
    assert "long_format_hierarchy_level_value" in _issues(schema, path, q, l1)
    assert _issues(schema, path, q, l1.replace(L1, L2)) == []


def test_counting_rows_needs_no_level_filter(stats):
    path, schema = stats
    assert _issues(schema, path, "How many rows are suppressed?", f"SELECT COUNT(*) FROM {T} WHERE suppressed = 'Y'") == []
    assert _issues(schema, path, "How many rows have no value?", f"SELECT COUNT(*) FROM {T} WHERE value IS NULL") == []


# ------------------------------------------------------------------------------------------- plan, reference, prompt
def test_plan_fixes_level_and_version_and_the_reference_matches(stats):
    path, schema = stats
    from modules.value_grounding import ground_question
    q = "What were the total unadjusted Sales for each top-level industry in the December 2025 quarter?"
    plan = build_plan(q, schema, ground_question(q, schema, path))
    structure = {f["column"]: f["value"] for f in plan["filters"] if f["source"] == "structure"}
    assert structure.get("group") == L1
    ref = reference_sql({**plan, "verifiable": True, "tables": [T], "group_by": [{"table": T, "column": "industry"}],
                         "date_range": None, "order": None, "limit": None,
                         "filters": plan["filters"] + [{"table": T, "column": "period", "op": "=", "value": 2025.12}]})
    con = sqlite3.connect(path)
    got = dict(con.execute(ref[0], ref[1]).fetchall())
    con.close()
    assert got == pytest.approx(_expected(L1, "Sales", "Unadjusted", 2025.12))


def test_prompt_describes_the_structure(stats):
    from modules.sql_generator import _schema_subset_text
    _, schema = stats
    text = _schema_subset_text(schema, [T])
    assert "LONG-FORMAT TABLE" in text and "HIERARCHY LEVEL" in text and "VERSIONS" in text
    assert text.index(repr(L1)) < text.index(repr(L2))           # top level listed first


# --------------------------------------------------------------------------------------- NULL-dropping inequality
def test_inequality_on_a_mostly_empty_column_is_flagged(stats):
    path, schema = stats
    bad = f"SELECT SUM(value) FROM {T} WHERE suppressed <> 'Y'"
    assert any(i["type"] == "intent_null_exclusion" for i in check_intent("Total value", bad, schema))
    ok = f"SELECT SUM(value) FROM {T} WHERE suppressed IS NULL OR suppressed <> 'Y'"
    assert not any(i["type"] == "intent_null_exclusion" for i in check_intent("Total value", ok, schema))
    full_col = f"SELECT SUM(value) FROM {T} WHERE variable <> 'Wages'"
    assert not any(i["type"] == "intent_null_exclusion" for i in check_intent("Total value", full_col, schema))
    con = sqlite3.connect(path)       # why it matters: NULL <> 'Y' is not true, so every unflagged row disappears
    kept = con.execute(f"SELECT COUNT(*) FROM {T} WHERE suppressed <> 'Y'").fetchone()[0]
    con.close()
    assert kept == 0 and sum(1 for r in ROWS if r[7] is None) == len(ROWS) - 2


# ------------------------------------------------------------------------------------- variables, labels, repairs
def test_a_variable_named_in_the_question_must_be_filtered(stats):
    path, schema = stats
    q = "Total unadjusted Mining sales in 2025"
    base = (f"SELECT SUM(value) FROM {T} WHERE industry = 'Mining' AND adjustment = 'Unadjusted' AND \"group\" = '{L1}' "
            "AND period >= 2025 AND period < 2026")
    assert "long_format_qualifier" in _issues(schema, path, q, base)
    assert _issues(schema, path, q, base + " AND variable = 'Sales'") == []
    # a question that names two variables is answered by combining them; no single filter is demanded
    q2 = "What share of top-level Sales did Wages represent in 2025, unadjusted?"
    both = (f"SELECT 100.0 * SUM(CASE WHEN variable = 'Wages' THEN value END) / SUM(CASE WHEN variable = 'Sales' THEN value END) "
            f"FROM {T} WHERE adjustment = 'Unadjusted' AND \"group\" = '{L1}' AND period >= 2025 AND period < 2026")
    assert _issues(schema, path, q2, both) == []


def test_the_level_column_is_not_a_name(stats):
    path, schema = stats
    sql = (f"SELECT \"group\" AS industry, value FROM {T} WHERE \"group\" = '{L1}' AND variable = 'Sales' "
           "AND adjustment = 'Unadjusted' AND period = 2025.12 ORDER BY value LIMIT 1")
    assert "long_format_level_label" in _issues(schema, path, "Which top-level industry had the lowest unadjusted sales?", sql)
    fixed = sql.replace('SELECT "group" AS industry', "SELECT industry")
    assert _issues(schema, path, "Which top-level industry had the lowest unadjusted sales?", fixed) == []


def test_missing_filters_are_added_without_an_llm_round_and_give_the_independent_answer(stats, monkeypatch):
    import json as _json
    import modules.sql_generator as sg
    from modules.semantic_layer import build_semantic_layer
    path, schema = stats
    calls = []
    model_sql = f"SELECT industry, SUM(value) AS s FROM {T} WHERE period = 2025.12 GROUP BY industry ORDER BY s DESC, industry LIMIT 1"

    def fake(system, user, key):
        calls.append(1)
        return _json.dumps({"sql": model_sql, "explanation": "", "confidence": "High", "complexity": "Simple"}), "groq:stub", []
    monkeypatch.setattr(sg, "_run_provider_chain", fake)
    out = sg.generate_sql(question="Which top-level industry had the highest unadjusted sales in the December 2025 quarter?",
                          schema=schema, api_key="k", semantic=build_semantic_layer(schema), retrieval=None, db_path=path)
    assert len(calls) == 1, "fixed deterministically, no repair call"
    assert any(f.startswith("filtered ") for f in out["deterministic_fixes"]), out["deterministic_fixes"]
    assert not [i for i in out["validation"].get("intent_warnings", []) if i["type"].startswith("long_format")]
    con = sqlite3.connect(path)
    got = con.execute(out["sql"]).fetchall()
    con.close()
    exp = _expected(L1, "Sales", "Unadjusted", 2025.12)
    best = sorted(exp.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    assert got == [(best[0], pytest.approx(best[1]))]


def test_self_join_on_the_key_is_valid_but_not_on_values(stats):
    from modules.sql_validator import validate_sql
    _, schema = stats
    key_join = (f"SELECT a.industry, 100.0 * a.value / b.value FROM {T} AS a JOIN {T} AS b ON a.period = b.period "
                f"AND a.industry = b.industry AND a.\"group\" = b.\"group\" AND a.adjustment = b.adjustment "
                f"WHERE a.variable = 'Wages' AND b.variable = 'Sales'")
    assert validate_sql(key_join, schema)["is_valid"]
    assert not validate_sql(f"SELECT a.industry FROM {T} AS a JOIN {T} AS b ON a.value = b.value", schema)["is_valid"]


def test_percentage_question_answered_with_a_bare_ratio_is_flagged(stats):
    _, schema = stats
    q = "What percentage of sales were wages?"
    ratio = f"SELECT SUM(CASE WHEN variable = 'Wages' THEN value END) / SUM(CASE WHEN variable = 'Sales' THEN value END) FROM {T}"
    assert any(i["type"] == "intent_percent_output" for i in check_intent(q, ratio, schema))
    assert not any(i["type"] == "intent_percent_output" for i in check_intent(q, ratio.replace("SELECT ", "SELECT 100.0 * "), schema))
