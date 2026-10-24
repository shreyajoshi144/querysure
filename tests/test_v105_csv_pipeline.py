"""CSV pipeline hardening (v1.0.5): exact-value grounding, planning, quoting, semantic validation, independent result
verification, controlled correction, honest reliability reporting. Expected values are computed with pandas straight
from the CSV file, never from the pipeline's own SQL."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "fixtures"))

import build_hard_csv as hc  # noqa: E402
from modules.data_reliability_agent import assess_reliability  # noqa: E402
from modules.ingestion import ingest_to_sqlite  # noqa: E402
from modules.query_planner import build_plan, check_sql_against_plan, reference_sql  # noqa: E402
from modules.query_result_engine import execute_query  # noqa: E402
from modules.result_verifier import correctness_dimensions, semantic_validate, verify_result  # noqa: E402
from modules.schema_engine import load_database  # noqa: E402
from modules.sql_params import parameterize_literals  # noqa: E402
from modules.sql_validator import validate_sql  # noqa: E402
from modules.sqlite_compile_check import autoquote_reserved  # noqa: E402
from modules.value_grounding import ground_question  # noqa: E402


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    d = tmp_path_factory.mktemp("hardcsv")
    rows = hc.make_rows()
    csv_path = d / "subs.csv"
    hc.write_csv(str(csv_path), rows)
    db = d / "subs.db"
    ingest_to_sqlite(str(csv_path), ".csv", str(db), "subs")
    df = pd.read_csv(csv_path, keep_default_na=True)
    return {"db": str(db), "schema": load_database(str(db)), "df": df, "csv": str(csv_path)}


def _val(res):
    return list(res["rows"][0].values())[0]


# ------------------------------------------------------------------ exact-value grounding (truncation root cause)
def test_long_categorical_value_is_never_truncated_in_grounding(env):
    hits = ground_question(f"What is the total amount for {hc.LONG_A}?", env["schema"], env["db"])
    assert [h["value"] for h in hits if h["column"] == "plan_name"] == [hc.LONG_A]      # full 78 chars, exact
    assert len(hc.LONG_A) > 40


def test_similar_long_values_are_distinguished(env):
    a = ground_question(f"amount for {hc.LONG_A}", env["schema"], env["db"])
    b = ground_question(f"amount for {hc.LONG_B}", env["schema"], env["db"])
    assert a[0]["value"] == hc.LONG_A and b[0]["value"] == hc.LONG_B


def test_high_cardinality_column_is_grounded_even_though_profile_lists_no_values(env):
    assert env["schema"]["tables"]["subs"]["column_stats"]["plan_name"]["distinct"] > 40
    assert "plan_name" not in env["schema"]["tables"]["subs"]["column_profile"]       # too many to list...
    assert ground_question("units for Standard Service Package 07 with extended coverage", env["schema"], env["db"])  # ...found anyway


def test_whitespace_case_apostrophe_and_accent_values_stay_exact(env):
    g = lambda q: [h["value"] for h in ground_question(q, env["schema"], env["db"]) if h["column"] == "customer"]  # noqa: E731
    assert g("units for Smith  and  Co") == ["Smith  and  Co"]
    assert g("units for smith and co") == ["smith and co"]
    assert g("amount for O'Brien & Sons Ltd") == ["O'Brien & Sons Ltd"]
    assert g("units for Zoe Cafe") == ["Zoë Café"]


def test_typo_resolves_to_closest_stored_value_marked_fuzzy(env):
    h = ground_question("units for Standard Service Pakage 07 with extended coverage", env["schema"], env["db"])
    assert h and h[0]["match"] == "fuzzy" and h[0]["value"] == "Standard Service Package 07 with extended coverage"


def test_prompt_contains_full_value_verbatim(env, monkeypatch):
    import modules.sql_generator as g
    seen = {}

    def fake(system, prompt, key):
        seen["prompt"] = prompt
        return (type("P", (), dict(sql="SELECT 1", explanation="", confidence="High", complexity="Simple"))(), "{}", "groq:x", [])
    monkeypatch.setattr(g, "_run_structured", fake)
    g.generate_sql(f"total amount for {hc.LONG_A}", env["schema"], "k", db_path=env["db"])
    assert hc.LONG_A in seen["prompt"] and "GROUNDED FILTER VALUES" in seen["prompt"] and "QUERY PLAN" in seen["prompt"]


# ------------------------------------------------------------------ reserved identifiers / quoting
def test_reserved_word_columns_unquoted_sql_is_rejected_before_execution(env):
    r = validate_sql("SELECT order, SUM(amount) FROM subs GROUP BY order", env["schema"])
    assert not r["is_valid"] and any(i["type"] == "sqlite_compile_error" for i in r["issues"])


def test_reserved_word_columns_quoted_sql_is_valid_and_executes(env):
    sql = 'SELECT "group", COUNT(*) AS n FROM subs GROUP BY "group" ORDER BY "group"'
    assert validate_sql(sql, env["schema"])["is_valid"]
    res = execute_query(sql, env["db"], parameterize=True)
    exp = env["df"].groupby("Group").size().sort_index()
    assert [(r["group"], r["n"]) for r in res["rows"]] == list(exp.items())


def test_autoquote_repairs_deterministically_and_only_when_it_compiles(env):
    fixed = autoquote_reserved("SELECT order, \"group\" FROM subs ORDER BY order LIMIT 3", env["schema"])
    assert fixed is not None and '"order"' in fixed and "ORDER BY" in fixed
    assert validate_sql(fixed, env["schema"])["is_valid"]
    assert autoquote_reserved("SELECT nonsense FROM nowhere", env["schema"]) is None


def test_generator_fixes_reserved_word_without_spending_an_llm_repair(env, monkeypatch):
    import modules.sql_generator as g
    monkeypatch.setattr(g, "_run_structured", lambda *a, **k: (type("P", (), dict(sql="SELECT order, units FROM subs", explanation="", confidence="High", complexity="Simple"))(), "{}", "groq:x", []))
    monkeypatch.setattr(g, "_repair_sql_with_llm", lambda *a, **k: pytest.fail("LLM repair must not be needed for quoting"))
    out = g.generate_sql("list order and units", env["schema"], "k")
    assert out["validation"]["is_valid"] and '"order"' in out["sql"] and out["deterministic_fixes"]


# ------------------------------------------------------------------ parameterized values
def test_values_with_quotes_are_bound_not_interpolated(env):
    sql = "SELECT SUM(units) AS u FROM subs WHERE customer = 'O''Brien & Sons Ltd'"
    q, params = parameterize_literals(sql)
    assert "?" in q and params == ["O'Brien & Sons Ltd"]
    res = execute_query(sql, env["db"], parameterize=True)
    exp = int(env["df"].loc[env["df"]["Customer"] == "O'Brien & Sons Ltd", "Units"].sum())
    assert _val(res) == exp and res["sql"].startswith("SELECT SUM(units)")


def test_hostile_literal_is_inert(env):
    sql = "SELECT COUNT(*) AS n FROM subs WHERE customer = 'x'' OR ''1''=''1'"
    assert _val(execute_query(sql, env["db"], parameterize=True)) == 0


# ------------------------------------------------------------------ semantic validation (pre-execution)
def test_missing_filter_is_caught(env):
    q = f"What is the total amount for {hc.LONG_A}?"
    r = semantic_validate(q, "SELECT SUM(amount) FROM subs", env["schema"], env["db"])
    assert any(i["type"] == "plan_filter" for i in r["issues"])


def test_truncated_filter_value_is_caught_with_the_exact_suggestion(env):
    sql = f"SELECT SUM(amount) FROM subs WHERE plan_name = '{hc.LONG_A[:40]}'"
    r = semantic_validate(f"total amount for {hc.LONG_A}", sql, env["schema"], env["db"])
    bad = [i for i in r["issues"] if i["type"] == "value_not_found"]
    assert bad and hc.LONG_A in bad[0]["suggestions"]


def test_wrong_case_value_is_caught(env):
    r = semantic_validate("count active subscriptions", "SELECT COUNT(*) FROM subs WHERE status = 'active'", env["schema"], env["db"])
    assert any(i["type"] == "value_not_found" and "Active" in i["suggestions"] for i in r["issues"])


def test_correct_exact_filter_passes_semantic_validation(env):
    sql = f"SELECT SUM(amount) FROM subs WHERE plan_name = '{hc.LONG_A}'"
    r = semantic_validate(f"What is the total amount for {hc.LONG_A}?", sql, env["schema"], env["db"])
    assert r["issues"] == []


def test_ranking_direction_and_limit_checked_against_plan(env):
    p = build_plan("Which 3 groups have the lowest total units?", env["schema"], [])
    assert p["order"]["direction"] == "asc" and p["limit"] == 3
    assert any(i["type"] == "plan_ordering" for i in check_sql_against_plan('SELECT "group", SUM(units) u FROM subs GROUP BY "group" ORDER BY u DESC LIMIT 3', p))
    assert any(i["type"] == "plan_limit" for i in check_sql_against_plan('SELECT "group", SUM(units) u FROM subs GROUP BY "group" ORDER BY u ASC', p))
    assert not check_sql_against_plan('SELECT "group", SUM(units) u FROM subs GROUP BY "group" ORDER BY u ASC LIMIT 3', p)


def test_equivalent_phrasings_produce_the_same_plan(env):
    qs = ["What is the total amount for Active subscriptions?", "Sum the amount where status is Active",
          "total amount of Active ones"]
    plans = []
    for q in qs:
        p = build_plan(q, env["schema"], ground_question(q, env["schema"], env["db"]))
        plans.append((p["aggregation"], p["measure"]["column"], [(f["column"], f["value"]) for f in p["filters"]]))
    assert len(set(map(str, plans))) == 1


# ------------------------------------------------------------------ independent verification
def _verify(env, q, sql):
    res = execute_query(sql, env["db"], parameterize=True)
    return verify_result(q, sql, env["schema"], res, env["db"], grounded=ground_question(q, env["schema"], env["db"])), res


def test_correct_answer_is_verified_by_independent_recomputation(env):
    q = "What is the total amount for Active subscriptions?"
    v, res = _verify(env, q, "SELECT SUM(amount) FROM subs WHERE status = 'Active'")
    assert v["status"] == "verified_match" and v["verified"] is True
    assert _val(res) == pytest.approx(env["df"].loc[env["df"]["Status"] == "Active", "Amount"].sum())   # pandas agrees


def test_nulls_are_ignored_by_aggregates_like_pandas(env):
    q = "What is the average amount?"
    v, res = _verify(env, q, "SELECT AVG(amount) FROM subs")
    assert v["verified"] and _val(res) == pytest.approx(env["df"]["Amount"].mean())
    assert env["df"]["Amount"].isna().sum() > 0


def test_wrong_filter_value_that_exists_is_a_detected_mismatch(env):
    q = "What is the total amount for Active subscriptions?"
    v, _ = _verify(env, q, "SELECT SUM(amount) FROM subs WHERE status = 'Pending'")
    assert v["status"] == "mismatch" and not v["verified"]


def test_wrong_aggregate_is_a_detected_mismatch(env):
    v, _ = _verify(env, "What is the average units by status?", 'SELECT status, SUM(units) FROM subs GROUP BY status')
    assert v["status"] == "mismatch"


def test_grouped_ranking_verified_and_wrong_order_detected(env):
    q = "Which group has the highest total units?"
    good, res = _verify(env, q, 'SELECT "group", SUM(units) AS u FROM subs GROUP BY "group" ORDER BY u DESC, "group" LIMIT 1')
    exp = env["df"].groupby("Group")["Units"].sum().sort_values(ascending=False)
    assert good["verified"] and res["rows"][0]["group"] == exp.index[0]
    bad, _ = _verify(env, q, 'SELECT "group", SUM(units) AS u FROM subs GROUP BY "group" ORDER BY u ASC LIMIT 1')
    assert bad["status"] == "mismatch"


def test_suspicious_empty_result_is_diagnosed_not_accepted(env):
    q = "total amount for Gold subscriptions"
    sql = "SELECT SUM(amount) FROM subs WHERE status = 'Gold'"
    res = execute_query(sql, env["db"], parameterize=True)
    v = verify_result(q, sql, env["schema"], res, env["db"])
    assert v["status"] == "suspicious_empty" and any("Gold" in d for d in v["diagnosis"])


def test_empty_but_plausible_result_is_marked_unconfirmed_not_verified(env):
    sql = "SELECT COUNT(*) AS n FROM subs WHERE status = 'Active' AND start_date > '2030-01-01'"
    # count returns a row (0); use a row-returning query to get a truly empty set
    sql = "SELECT units FROM subs WHERE status = 'Active' AND start_date > '2030-01-01'"
    res = execute_query(sql, env["db"], parameterize=True)
    v = verify_result("units of active subscriptions after 2030", sql, env["schema"], res, env["db"])
    assert res["row_count"] == 0 and v["status"] == "empty_plausible" and v["verified"] is False


def test_unsupported_question_shape_is_not_claimed_verified(env):
    v, _ = _verify(env, "Show the top 5 orders by amount", 'SELECT "order", amount FROM subs ORDER BY amount DESC LIMIT 5')
    assert v["status"] == "not_independently_verified" and v["verified"] is False


def test_date_range_inclusive_end_is_verified(env):
    q = "What is the total units between 2023-03-01 and 2023-03-31?"
    sql = "SELECT SUM(units) FROM subs WHERE start_date BETWEEN '2023-03-01' AND '2023-03-31'"
    v, res = _verify(env, q, sql)
    df = env["df"]
    exp = df[(df["Start Date"] >= "2023-03-01") & (df["Start Date"] <= "2023-03-31")]["Units"].sum()
    assert v["verified"] and _val(res) == exp
    bad, _ = _verify(env, q, "SELECT SUM(units) FROM subs WHERE start_date >= '2023-03-01' AND start_date < '2023-03-31'")
    assert bad["status"] == "mismatch"   # exclusive end drops 03-31


# ------------------------------------------------------------------ honest reliability reporting
def test_execution_success_is_not_reported_as_correctness(env):
    q = "What is the total amount for Active subscriptions?"
    sql = "SELECT SUM(amount) FROM subs WHERE status = 'Pending'"
    v, res = _verify(env, q, sql)
    res["verification"], res["semantic_validation"] = v, None
    val = validate_sql(sql, env["schema"])
    dims = correctness_dimensions(val, None, res, v)
    assert dims["syntax"]["status"] == "pass" and dims["security"]["status"] == "pass" and dims["execution"]["status"] == "pass"
    assert dims["result_correctness"]["status"] == "mismatch"


def test_unverified_answer_never_gets_a_pass_for_correctness(env):
    res = execute_query("SELECT status, COUNT(*) FROM subs GROUP BY status", env["db"])
    dims = correctness_dimensions(validate_sql("SELECT status, COUNT(*) FROM subs GROUP BY status", env["schema"]), None, res, None)
    assert dims["result_correctness"]["status"] == "not_verified" and dims["semantic_validation"]["status"] == "not_run"


def test_failed_independent_check_caps_the_reliability_score(env):
    q = "total amount for Gold subscriptions"
    sql = "SELECT SUM(amount) FROM subs WHERE status = 'Gold'"
    res = execute_query(sql, env["db"])
    res["verification"] = {"status": "suspicious_empty", "verified": False, "checks": [{"check": "x", "result": "suspicious", "detail": "d"}]}
    rel = assess_reliability(env["schema"], validate_sql(sql, env["schema"]), res)
    assert rel["reliability_score"] <= 60 and rel["capped_reason"] and rel["answer_verified"] is False
    assert "not a measure of answer accuracy" in rel["score_meaning"]


# ------------------------------------------------------------------ controlled correction
def _gen(env, monkeypatch, first_sql, repairs, question):
    import modules.sql_generator as g
    calls = []
    monkeypatch.setattr(g, "_run_structured", lambda *a, **k: (type("P", (), dict(sql=first_sql, explanation="", confidence="High", complexity="Simple"))(), "{}", "groq:x", []))

    def fake_repair(q, sql, feedback, *a, **k):
        calls.append(feedback)
        return repairs[min(len(calls) - 1, len(repairs) - 1)], "groq:x", []
    monkeypatch.setattr(g, "_repair_sql_with_llm", fake_repair)
    return g.generate_sql(question, env["schema"], "k", db_path=env["db"]), calls


def test_repair_is_driven_by_the_diagnosed_cause_and_revalidated(env, monkeypatch):
    q = f"What is the total amount for {hc.LONG_A}?"
    good = f"SELECT SUM(amount) FROM subs WHERE plan_name = '{hc.LONG_A}'"
    out, calls = _gen(env, monkeypatch, f"SELECT SUM(amount) FROM subs WHERE plan_name = '{hc.LONG_A[:40]}'", [good], q)
    assert len(calls) == 1 and "does not exist" in str(calls[0]) and hc.LONG_A in str(calls[0])
    assert out["sql"] == good and out["validation"]["is_valid"] and out["verification"]["verified"] is True


def test_correction_is_bounded_and_never_fabricates(env, monkeypatch):
    q = f"What is the total amount for {hc.LONG_A}?"
    bad1 = f"SELECT SUM(amount) FROM subs WHERE plan_name = '{hc.LONG_A[:30]}'"
    bad2 = f"SELECT SUM(amount) FROM subs WHERE plan_name = '{hc.LONG_A[:20]}'"
    out, calls = _gen(env, monkeypatch, bad1, [bad2, bad1, bad2], q)
    assert len(calls) <= 2                                   # never retries indefinitely
    assert out["verification"] is None or out["verification"]["verified"] is False
    assert out["validation"]["intent_warnings"]              # the remaining problem is reported, not hidden


def test_failed_repair_never_replaces_a_valid_query(env, monkeypatch):
    q = "total amount for Active subscriptions"
    first = "SELECT SUM(amount) FROM subs WHERE status = 'Pending'"
    out, calls = _gen(env, monkeypatch, first, ["SELECT nope FROM nowhere"], q)
    assert out["validation"]["is_valid"] and out["sql"] in (first,)


# ------------------------------------------------------------------ regression: previous behaviours
def test_original_two_percent_and_date_behaviour_still_holds(env):
    prof = env["schema"]["tables"]["subs"]["column_profile"]
    assert prof["start_date"]["kind"] == "date_text" and prof["status"]["kind"] == "categorical"
