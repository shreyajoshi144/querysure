"""v1.0.7 final release: regressions for the fixes made while preparing the public deploy."""
import os
import sqlite3
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RETAIL = os.path.join(ROOT, "retail.db")


@pytest.fixture(scope="module")
def retail():
    if not os.path.exists(RETAIL):
        subprocess.run([sys.executable, "create_retail.py"], cwd=ROOT, check=True, capture_output=True)
    from modules.schema_engine import load_database
    return RETAIL, load_database(RETAIL)


def _verify(question, sql, retail):
    from modules.query_result_engine import execute_query
    from modules.result_verifier import semantic_validate, verify_result
    db, schema = retail
    sem = semantic_validate(question, sql, schema, db)
    return sem, verify_result(question, sql, schema, execute_query(sql, db), db, plan=sem.get("plan"))


# ---- verifier false alarms on correct SQL ---------------------------------------------------------------------
def test_counting_one_table_grouped_by_another_is_not_reported_as_mismatch(retail):
    sql = ("SELECT s.region, COUNT(o.order_id) AS orders FROM orders AS o JOIN stores AS s ON s.store_id = o.store_id "
           "GROUP BY s.region ORDER BY orders DESC")
    sem, v = _verify("How many orders per store region?", sql, retail)
    assert v["status"] != "mismatch"
    assert sem["plan"]["verifiable"] is False
    assert any("join required" in n for n in sem["plan"]["notes"])


def test_count_threshold_on_the_aggregate_is_not_modelled_as_a_reference(retail):
    sql = ("SELECT loyalty_tier, COUNT(*) AS customers FROM customers GROUP BY loyalty_tier "
           "HAVING COUNT(*) > 120 ORDER BY loyalty_tier")
    _, v = _verify("How many customers does each loyalty tier have? Only show tiers with more than 120 customers.", sql, retail)
    assert v["status"] != "mismatch"


def test_period_the_planner_cannot_model_disables_the_reference(retail):
    from modules.query_planner import build_plan
    _, schema = retail
    plan = build_plan("How many completed orders were placed between 2023-01-01 and 2023-06-30, inclusive?", schema, [])
    assert plan["date_range"] or plan["verifiable"] is False


def test_at_least_is_a_threshold_not_a_ranking(retail):
    from modules.query_planner import build_plan
    from modules.sql_intent_checker import check_intent
    _, schema = retail
    q = "List the product name and cost price of products whose sell price is at least twice their cost price."
    assert build_plan(q, schema, [])["order"] is None
    issues = check_intent(q, "SELECT p.product_name, p.cost_price FROM products AS p WHERE p.sell_price >= 2 * p.cost_price", schema)
    assert not [i for i in issues if i["type"] == "intent_ranking"]


def test_how_many_days_is_a_duration_not_a_row_count(retail):
    from modules.query_planner import build_plan
    from modules.sql_intent_checker import check_intent
    _, schema = retail
    q = "How many whole days passed between the earliest and the latest order date?"
    assert build_plan(q, schema, [])["aggregation"] != "count"
    sql = "SELECT CAST(julianday(MAX(o.order_date)) - julianday(MIN(o.order_date)) AS INTEGER) AS days FROM orders AS o"
    assert not [i for i in check_intent(q, sql, schema) if i["type"] == "intent_aggregate"]


def test_a_real_mismatch_is_still_caught(retail):
    # Counting stores when the question asks how many stores are in each region: reference applies, wrong SQL differs.
    sql = "SELECT s.region, COUNT(*) + 1 AS stores FROM stores AS s GROUP BY s.region"
    _, v = _verify("How many stores are in each region?", sql, retail)
    assert v["status"] == "mismatch"


# ---- answer headline ------------------------------------------------------------------------------------------
def test_headline_states_what_the_result_shows():
    from modules.business_insight_agent import generate_insights
    qr = {"rows": [{"region": "North", "revenue": 160932.48}, {"region": "South", "revenue": 68720.75}],
          "columns": ["region", "revenue"], "status": "success", "result_profile": {}}
    out = generate_insights({}, qr, {"reliability_score": 95, "reliability_level": "High"})
    assert out["headline"].startswith("North has the highest revenue at 160,932.48")
    assert "South has the lowest" in out["headline"]
    assert out["executive_note"].startswith(out["headline"])
    assert "suitable for business review" not in out["executive_note"]


def test_single_value_headline():
    from modules.business_insight_agent import generate_insights
    out = generate_insights({}, {"rows": [{"orders": 3000}], "columns": ["orders"], "status": "success"}, None)
    assert out["headline"] == "Result: orders: 3,000."


# ---- execution: truncated flag -----------------------------------------------------------------------------------
def test_truncated_only_when_rows_were_actually_cut(tmp_path):
    from modules.query_result_engine import execute_query
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(10)])
    con.commit(); con.close()
    exact = execute_query("SELECT t.x FROM t", str(db), max_rows=10)
    assert exact["row_count"] == 10 and exact["execution_metadata"]["truncated"] is False
    over = execute_query("SELECT t.x FROM t", str(db), max_rows=9)
    assert over["row_count"] == 9 and over["execution_metadata"]["truncated"] is True
    assert "LIMIT 9" in over["sql"]                     # reported SQL is unchanged by the extra-row probe
    own = execute_query("SELECT t.x FROM t LIMIT 50", str(db), max_rows=5)
    assert own["row_count"] == 5 and own["execution_metadata"]["truncated"] is True


# ---- startup: SQLite create_all race ------------------------------------------------------------------------------
def test_sqlite_init_db_survives_a_concurrent_worker_creating_the_same_table(monkeypatch):
    import db as dbmod
    from sqlalchemy.exc import OperationalError
    if dbmod.engine.dialect.name != "sqlite" or dbmod.IS_PRODUCTION:
        pytest.skip("SQLite development path only")
    calls = {"n": 0}
    real = dbmod.Base.metadata.create_all

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("CREATE TABLE subscriptions", {}, Exception("table subscriptions already exists"))
        return real(*a, **kw)

    monkeypatch.setattr(dbmod.Base.metadata, "create_all", flaky)
    dbmod.init_db()
    assert calls["n"] == 2


def test_sqlite_init_db_does_not_swallow_other_errors(monkeypatch):
    import db as dbmod
    from sqlalchemy.exc import OperationalError
    if dbmod.engine.dialect.name != "sqlite" or dbmod.IS_PRODUCTION:
        pytest.skip("SQLite development path only")
    monkeypatch.setattr(dbmod.Base.metadata, "create_all",
                        lambda *a, **kw: (_ for _ in ()).throw(OperationalError("x", {}, Exception("disk I/O error"))))
    with pytest.raises(OperationalError):
        dbmod.init_db()


# ---- LLM configuration --------------------------------------------------------------------------------------------
def test_default_models_are_not_the_retired_ones():
    import config
    retired = {"llama-3.3-70b-versatile", "llama-3.1-8b-instant", "claude-3-5-haiku-20241022"}
    assert not retired & {config.GROQ_MODEL, config.GROQ_FALLBACK_MODEL, config.ANTHROPIC_FALLBACK_MODEL}
    for m in (config.GROQ_MODEL, config.GROQ_FALLBACK_MODEL, config.OPENAI_FALLBACK_MODEL, config.ANTHROPIC_FALLBACK_MODEL):
        assert m in config.LLM_MODEL_PRICES_USD_PER_MTOK, f"default model {m} has no price entry"


def test_groq_reasoning_models_get_reasoning_effort_and_room_for_output(monkeypatch):
    import groq
    from modules import sql_generator as sg
    seen = {}

    class _Resp:
        choices = [type("C", (), {"message": type("M", (), {"content": '{"sql": "SELECT 1"}'})()})()]
        usage = None

    class _Fake:
        def __init__(self, **kw):
            self.chat = type("Chat", (), {"completions": type("Comp", (), {"create": staticmethod(lambda **k: seen.update(k) or _Resp())})()})()

    monkeypatch.setattr(groq, "Groq", _Fake)
    sg._call_groq("openai/gpt-oss-120b", "k", "sys", "user")
    assert seen["extra_body"] == {"reasoning_effort": "low"}
    assert seen["max_tokens"] >= 2000 and seen["response_format"] == {"type": "json_object"}
    seen.clear()
    sg._call_groq("some-other-model", "k", "sys", "user")
    assert "extra_body" not in seen


def test_production_starts_without_google_client_id():
    env = {"PATH": os.environ["PATH"], "HOME": "/tmp", "JWT_SECRET_KEY": "k" * 48, "ENVIRONMENT": "production",
           "DATABASE_URL": "postgresql+psycopg2://u:p@h/db", "CORS_ALLOWED_ORIGINS": "https://a.example.com",
           "EMAIL_PROVIDER": "smtp", "SMTP_HOST": "h", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "p", "EMAIL_FROM": "a@b.co",
           "APP_BASE_URL": "https://a.example.com", "TRUSTED_PROXY_IPS": "10.0.0.1", "REDIS_URL": "redis://x:6379/0",
           "STORAGE_BACKEND": "s3", "S3_BUCKET": "b"}
    r = subprocess.run([sys.executable, "-c", "import config"], env=env, cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-400:]
    assert "Google sign-in is disabled" in r.stderr


def test_env_example_works_for_a_first_local_run():
    text = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
    assert "\nEMAIL_PROVIDER=dev\n" in text          # sign-up is possible without an SMTP account
    assert "\nWEB_CONCURRENCY=1\n" in text
