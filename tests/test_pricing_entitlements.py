from pathlib import Path

from db import PipelineSession, SessionLocal

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")


def test_plan_defaults_and_limits():
    from entitlements import PLAN_CONFIG, DEFAULT_PLAN

    assert DEFAULT_PLAN == "free"
    assert PLAN_CONFIG["free"]["daily_query_limit"] == 10
    assert PLAN_CONFIG["pro"]["daily_query_limit"] == 50
    assert PLAN_CONFIG["team"]["daily_query_limit"] == 100
    assert PLAN_CONFIG["business"]["daily_query_limit"] is None


def test_settings_and_pricing_surfaces_are_wired():
    assert 'id="settingsPlanName"' in HTML
    assert 'id="settingsQueryUsage"' in HTML
    assert 'id="settingsQueriesRemaining"' in HTML
    assert 'href="#pricing"' in HTML
    assert 'id="security"' in HTML
    assert 'data-site-route="/security"' in HTML
    assert HTML.count('<article class="pricing-plan') == 4
    assert "apiGet('/api/account/usage')" in JS


def test_authenticated_usage_starts_at_free(client, auth_headers):
    response = client.get("/api/account/usage", headers=auth_headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["plan"] == "free"
    assert data["daily_limit"] == 10
    assert data["used_today"] == 0
    assert data["remaining_today"] == 10
    assert data["reset_at"]


def test_execute_sql_consumes_one_slot_and_enforces_limit(client, auth_headers, monkeypatch):
    import api as api_module
    import storage

    session_response = client.post("/api/session", headers=auth_headers, json={})
    assert session_response.status_code == 200, session_response.text
    session_id = session_response.json()["session_id"]

    db = SessionLocal()
    try:
        row = db.query(PipelineSession).filter(PipelineSession.id == session_id).one()
        row.db_path = "/tmp/querysure-test.db"
        row.schema = {"tables": {"orders": {"columns": {"id": "INTEGER"}}}}
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(api_module, "validate_sql", lambda *args, **kwargs: {"is_valid": True, "validation_score": 100})
    monkeypatch.setattr(storage, "ensure_local", lambda *args, **kwargs: "/tmp/querysure-test.db")
    monkeypatch.setattr(api_module, "execute_query", lambda *args, **kwargs: {"success": True, "rows": [], "row_count": 0})

    for _ in range(10):
        response = client.post(
            "/api/execute-sql",
            headers=auth_headers,
            json={"session_id": session_id, "sql": "SELECT id FROM orders"},
        )
        assert response.status_code == 200, response.text

    blocked = client.post(
        "/api/execute-sql",
        headers=auth_headers,
        json={"session_id": session_id, "sql": "SELECT id FROM orders"},
    )
    assert blocked.status_code == 429, blocked.text
    data = blocked.json()
    assert data["code"] == "QUERY_LIMIT_REACHED"
    assert data["plan"] == "free"
    assert data["daily_limit"] == 10
    assert data["used_today"] == 10
    assert data["remaining"] == 0
