from __future__ import annotations

import os

from modules.schema_engine import load_database
from modules.semantic_layer import build_semantic_layer
from modules.query_result_engine import execute_query
from modules.data_reliability_agent import assess_reliability
from modules.business_insight_agent import generate_insights
from modules.sql_validator import validate_sql


def test_health_endpoint_still_works(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json().get("status") in ("ok", "healthy", "up") or r.json().get("success") is not False


def test_ready_endpoint_still_works(client):
    r = client.get("/api/ready")
    # /api/ready checks live DB (and Redis if configured); in this dev/test
    # environment it should at least respond (200 or a structured 503), not
    # crash with a 500.
    assert r.status_code in (200, 503)


def test_auth_register_login_me_flow_still_works(client, auth_headers):
    r = client.get("/api/auth/me", headers=auth_headers)
    assert r.status_code == 200
    assert "@" in r.json()["email"]


def test_auth_required_on_pipeline_routes(client):
    r = client.post("/api/session")
    assert r.status_code == 401


def _make_schema_and_result(tmp_path):
    import sqlite3
    db_path = os.path.join(tmp_path, "reg.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, region TEXT, amount REAL)")
    conn.executemany("INSERT INTO t VALUES (?,?,?)", [
        (1, "North", 10.0), (2, "South", 20.0), (3, "North", 30.0),
    ])
    conn.commit()
    conn.close()
    schema = load_database(db_path)
    sql = "SELECT region, SUM(amount) AS total FROM t GROUP BY region"
    validation = validate_sql(sql, schema)
    query_result = execute_query(sql, db_path)
    return db_path, schema, validation, query_result


def test_reliability_flow_still_works(tmp_path):
    _, schema, validation, query_result = _make_schema_and_result(str(tmp_path))
    reliability = assess_reliability(schema, validation, query_result)
    # assess_reliability returns an overall reliability_score/level/status,
    # plus one sub-dict per dimension (completeness, freshness, integrity,
    # drift, uniqueness), each with its own score/status/details.
    assert "reliability_score" in reliability
    assert "reliability_level" in reliability
    for dim in ("completeness", "freshness", "integrity", "drift", "uniqueness"):
        assert "score" in reliability[dim], f"{dim} missing a score"
        assert "status" in reliability[dim], f"{dim} missing a status"


def test_insights_flow_still_works(tmp_path):
    _, schema, validation, query_result = _make_schema_and_result(str(tmp_path))
    reliability = assess_reliability(schema, validation, query_result)
    insights = generate_insights(schema, query_result, reliability, semantic=build_semantic_layer(schema))
    assert isinstance(insights, dict)


def test_report_generation_still_works(tmp_path):
    from modules.report_generator import generate_report

    db_path, schema, validation, query_result = _make_schema_and_result(str(tmp_path))
    reliability = assess_reliability(schema, validation, query_result)
    semantic = build_semantic_layer(schema)
    insights = generate_insights(schema, query_result, reliability, semantic=semantic)
    report = generate_report(
        question="What is total amount by region?",
        sql_result={"sql": query_result["sql"]},
        validation_result=validation,
        query_result=query_result,
        reliability_result=reliability,
        insight_result=insights,
        semantic=semantic,
        output_dir=str(tmp_path / "output"),
    )
    assert report.get("success") is True
    assert os.path.exists(report["html_path"])
    assert os.path.exists(report["json_path"])
