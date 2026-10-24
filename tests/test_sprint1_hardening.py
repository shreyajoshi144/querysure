import os
import subprocess
import sys

import pytest

import api as api_module


MALFORMED_JSON_ENDPOINTS = [
    "/api/auth/login",
    "/api/session/flush",
    "/api/connect/sample",
    "/api/schema-intelligence",
    "/api/semantic-layer",
    "/api/retrieve-tables",
    "/api/generate-sql",
    "/api/validate-sql",
    "/api/execute-sql",
    "/api/analyze-reliability",
    "/api/generate-insights",
    "/api/generate-report",
]


@pytest.mark.parametrize("endpoint", MALFORMED_JSON_ENDPOINTS)
def test_malformed_json_is_422_with_stable_error_shape(client, auth_headers, endpoint):
    headers = {"Content-Type": "application/json"}
    if endpoint != "/api/auth/login":
        headers.update(auth_headers)
    response = client.post(endpoint, headers=headers, content='{"broken":')
    assert response.status_code == 422, response.text
    payload = response.json()
    assert payload["success"] is False
    assert payload["error_code"] == "INVALID_REQUEST"
    assert payload.get("request_id")


def test_security_headers_in_production_mode(client, monkeypatch):
    monkeypatch.setattr(api_module, "IS_PRODUCTION", True)
    monkeypatch.setattr(api_module, "CSP_REPORT_ONLY", False)
    response = client.get("/api/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "camera=()" in response.headers["Permissions-Policy"]
    assert "max-age=31536000" in response.headers["Strict-Transport-Security"]
    assert "unsafe-inline" not in response.headers["Content-Security-Policy"].split("script-src", 1)[1].split(";", 1)[0]


def test_production_docs_are_disabled_by_app_configuration():
    env = os.environ.copy()
    env.update({
        "ENVIRONMENT": "production",
        "JWT_SECRET_KEY": "test-production-secret",
        "EMAIL_PROVIDER": "smtp",
        "SMTP_HOST": "smtp.example.test",
        "SMTP_USERNAME": "user",
        "SMTP_PASSWORD": "password",
        "EMAIL_FROM": "QuerySure <no-reply@example.test>",
        "APP_BASE_URL": "https://app.example.test",
        "GOOGLE_CLIENT_ID": "client-id",
        "DATABASE_URL": "postgresql+psycopg2://user:pass@localhost:5432/querysure",
        "REDIS_URL": "redis://localhost:6379/0",
        "S3_BUCKET": "querysure-test",
        "CORS_ALLOWED_ORIGINS": "https://app.example.test",
        "STORAGE_BACKEND": "s3",
        "TRUSTED_PROXY_IPS": "10.0.0.0/8",
    })
    code = "import api; print(api.app.docs_url, api.app.redoc_url, api.app.openapi_url)"
    result = subprocess.run([sys.executable, "-c", code], cwd=api_module.ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "None None None"


def test_change_password_invalidates_old_token(client, auth_headers):
    import uuid
    from tests.auth_test_utils import verify_registered_email

    # Create a fresh account rather than mutating the fixture account.
    email = f"pw-{uuid.uuid4().hex[:10]}@example.com"
    old_password = "OldPassword123!"
    new_password = "NewPassword456!"
    r = client.post("/api/auth/register", json={"email": email, "password": old_password})
    assert r.status_code == 200
    verify_registered_email(client, email)
    r = client.post("/api/auth/login", json={"email": email, "password": old_password})
    assert r.status_code == 200
    old_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = client.post("/api/auth/change-password", headers=old_headers, json={
        "current_password": old_password,
        "new_password": new_password,
    })
    assert r.status_code == 200, r.text
    new_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/api/auth/me", headers=old_headers).status_code == 401
    assert client.get("/api/auth/me", headers=new_headers).status_code == 200


def test_generate_report_rate_limit_returns_429(client, auth_headers, monkeypatch):
    previous = api_module.limiter.enabled
    api_module.limiter.enabled = True
    try:
        statuses = [
            client.post("/api/generate-report", headers=auth_headers, json={}).status_code
            for _ in range(11)
        ]
    finally:
        api_module.limiter.enabled = previous
    assert 429 in statuses
