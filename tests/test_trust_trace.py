from tests.auth_test_utils import verify_registered_email
from types import SimpleNamespace

from modules.trust_trace import build_trust_trace


def _row(**overrides):
    base = dict(
        question="What is total revenue?",
        semantic={
            "tables": {
                "orders": {
                    "business_name": "Orders",
                    "table_summary": "Order facts",
                    "columns": [{"column": "revenue", "meaning": "Total sales value", "semantic_role": "metric", "domain": "Finance"}],
                }
            }
        },
        retrieval={
            "top_tables": [{"table": "orders", "business_name": "Orders", "table_type": "fact_table", "score": 0.91}],
            "top_columns": [{"table": "orders", "column": "revenue", "role": "metric", "score": 0.88}],
            "join_hints": [],
            "vector_query": [{"secret": "should-not-leak"}],
        },
        sql="SELECT SUM(revenue) AS total_revenue FROM orders",
        validation={"is_valid": True, "validation_score": 98, "risk_score": 2, "join_validation": {"valid": True}, "aggregation_validation": {"valid": True}, "warnings": [], "issues": []},
        query_result={"success": True, "status": "success", "row_count": 1, "rows": [{"total_revenue": 42}], "execution_metadata": {"execution_time_ms": 7}},
        reliability={"reliability_score": 96, "reliability_level": "High", "status": "PASS", "integrity": {"score": 98}},
        insights={"executive_note": "Revenue is 42."},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _contains_secret(value):
    if isinstance(value, dict):
        return any(_contains_secret(k) or _contains_secret(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_contains_secret(v) for v in value)
    return isinstance(value, str) and ("secret" in value.lower() or "api_key" in value.lower() or "prompt" in value.lower())


def test_trace_uses_existing_pipeline_data():
    trace = build_trust_trace(_row())
    assert trace["question"] == "What is total revenue?"
    assert trace["grounding"]["tables"][0]["table"] == "orders"
    assert trace["semantic_context"][0]["columns"][0]["meaning"] == "Total sales value"
    assert trace["sql"].startswith("SELECT SUM")
    assert trace["validation"]["is_valid"] is True
    assert trace["execution"]["execution_time_ms"] == 7
    assert trace["execution"]["rows_returned"] == 1
    assert trace["reliability"]["reliability_score"] == 96
    assert trace["answer"] == "Revenue is 42."


def test_trace_omits_missing_optional_fields():
    row = _row(semantic=None, retrieval=None, sql=None, validation=None, query_result=None, reliability=None, insights=None)
    trace = build_trust_trace(row)
    assert trace["question"] == "What is total revenue?"
    for key in ("grounding", "semantic_context", "sql", "validation", "execution", "reliability", "answer"):
        assert key not in trace


def test_trace_does_not_expose_secret_or_internal_retrieval_payloads():
    trace = build_trust_trace(_row())
    assert "vector_query" not in str(trace)
    assert not _contains_secret(trace)


def test_trace_does_not_cross_contaminate_session_data():
    trace_a = build_trust_trace(_row(question="A question", sql="SELECT 1", insights={"executive_note": "A answer"}))
    trace_b = build_trust_trace(_row(question="B question", sql="SELECT 2", insights={"executive_note": "B answer"}))
    assert trace_a["question"] == "A question"
    assert trace_a["sql"] == "SELECT 1"
    assert trace_a["answer"] == "A answer"
    assert trace_b["question"] == "B question"
    assert trace_b["sql"] == "SELECT 2"
    assert trace_b["answer"] == "B answer"


def test_answer_trace_endpoint_is_session_isolated(client, auth_headers):
    session = client.post('/api/session', headers=auth_headers)
    assert session.status_code == 200
    owner_session_id = session.json()['session_id']

    other_email = 'other-trace-user@example.com'
    register = client.post('/api/auth/register', json={'email': other_email, 'password': 'TestPassword123!'})
    assert register.status_code == 200
    verify_registered_email(client, other_email)
    login = client.post('/api/auth/login', json={'email': other_email, 'password': 'TestPassword123!'})
    assert login.status_code == 200
    other_headers = {'Authorization': 'Bearer ' + login.json()['access_token']}

    response = client.get(f'/api/answer-trace?session_id={owner_session_id}', headers=other_headers)
    # Sprint 2: foreign sessions are never readable or auto-created -> 404 SESSION_NOT_FOUND.
    assert response.status_code == 404
    assert response.json()['error_code'] == 'SESSION_NOT_FOUND'
    assert owner_session_id not in response.text
