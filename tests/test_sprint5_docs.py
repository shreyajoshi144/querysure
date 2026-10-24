"""Sprint 5: documentation must match the implementation, and the production start-up guards must be real."""
import glob
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_READ = re.compile(r'(?:os\.environ\.get|os\.getenv|_env_number|_env_flag|_env_float|_env_int)\(\s*"([A-Z][A-Z0-9_]+)"')


def _env_vars_read_by_code():
    names = set()
    for f in glob.glob(os.path.join(ROOT, "*.py")) + glob.glob(os.path.join(ROOT, "observability", "*.py")) + glob.glob(os.path.join(ROOT, "modules", "*.py")):
        names |= set(_READ.findall(open(f, encoding="utf-8").read()))
    return names


def test_every_environment_variable_is_documented():
    doc = open(os.path.join(ROOT, "docs", "ENVIRONMENT.md"), encoding="utf-8").read()
    missing = sorted(n for n in _env_vars_read_by_code() if f"`{n}`" not in doc)
    assert not missing, f"undocumented environment variables: {missing}"


def test_env_example_lists_the_billing_and_session_settings():
    example = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
    for name in ("PAYMENT_PROVIDER", "PAYMENT_WEBHOOK_SECRETS", "BILLING_GRACE_HOURS", "AUTH_REQUIRE_SESSION_ID", "AUTH_MAX_ACTIVE_SESSIONS"):
        assert re.search(rf"^{name}=", example, re.M), name


def test_documented_api_routes_exist():
    import api
    from tests.route_utils import iter_api_routes
    real = {(m, path) for r, path, _ in iter_api_routes(api.app.routes) for m in r.methods}
    doc = open(os.path.join(ROOT, "docs", "API.md"), encoding="utf-8").read()
    documented = set(re.findall(r"\| (GET|POST|DELETE|PUT|PATCH) \| `(/api[^`]*)` \|", doc))
    assert documented, "no routes parsed from docs/API.md"
    assert not (documented - real), f"documented but not implemented: {sorted(documented - real)}"
    undocumented = {(m, p) for m, p in real if p.startswith("/api") and m in ("GET", "POST", "DELETE", "PUT", "PATCH")} - documented
    assert not undocumented, f"implemented but undocumented: {sorted(undocumented)}"


_PROD = dict(
    ENVIRONMENT="production", DATABASE_URL="postgresql+psycopg2://u:p@h/db", CORS_ALLOWED_ORIGINS="https://a.example.com",
    EMAIL_PROVIDER="smtp", SMTP_HOST="h", SMTP_USERNAME="u", SMTP_PASSWORD="p", EMAIL_FROM="a@b.co",
    APP_BASE_URL="https://a.example.com", TRUSTED_PROXY_IPS="10.0.0.1", REDIS_URL="redis://x:6379/0",
    GOOGLE_CLIENT_ID="g.apps.googleusercontent.com", STORAGE_BACKEND="s3", S3_BUCKET="b",
)


def _starts(**overrides):
    env = {"PATH": os.environ["PATH"], "HOME": "/tmp", "JWT_SECRET_KEY": "k" * 48, **_PROD, **overrides}
    env = {k: v for k, v in env.items() if v is not None}
    r = subprocess.run([sys.executable, "-c", "import config"], env=env, cwd=ROOT, capture_output=True, text=True)
    return r.returncode == 0, r.stderr


def test_valid_production_environment_starts():
    ok, err = _starts()
    assert ok, err[-400:]


@pytest.mark.parametrize("secret,should_start", [("short", False), ("k" * 15, False), ("k" * 16, True), ("k" * 32, True)])
def test_production_refuses_weak_jwt_secret(secret, should_start):
    ok, err = _starts(JWT_SECRET_KEY=secret)
    assert ok is should_start, err[-300:]
    if ok and len(secret) < 32:
        assert "shorter than 32 characters" in err  # starts, but loudly


@pytest.mark.parametrize("overrides,fragment", [
    ({"CORS_ALLOWED_ORIGINS": "*"}, "CORS_ALLOWED_ORIGINS"),
    ({"PAYMENT_PROVIDER": "generic"}, "PAYMENT_WEBHOOK_SECRETS"),
    ({"PAYMENT_PROVIDER": "generic", "PAYMENT_WEBHOOK_SECRETS": "s" * 20, "PAYMENT_CHECKOUT_URL": "http://x"}, "HTTPS"),
    ({"REDIS_URL": None}, "REDIS_URL"),
    ({"S3_BUCKET": None}, "S3_BUCKET"),
])
def test_production_guards_refuse_unsafe_configuration(overrides, fragment):
    ok, err = _starts(**overrides)
    assert not ok and fragment in err, err[-300:]


def test_maintenance_cron_declares_every_variable_the_production_guards_need():
    import yaml
    spec = yaml.safe_load(open(os.path.join(ROOT, "render.yaml"), encoding="utf-8"))
    cron = next(s for s in spec["services"] if s["type"] == "cron")
    keys = {e["key"] for e in cron["envVars"]}
    required = {"ENVIRONMENT", "JWT_SECRET_KEY", "DATABASE_URL", "CORS_ALLOWED_ORIGINS", "SMTP_HOST", "SMTP_USERNAME",
                "SMTP_PASSWORD", "EMAIL_FROM", "APP_BASE_URL", "GOOGLE_CLIENT_ID", "TRUSTED_PROXY_IPS", "REDIS_URL", "S3_BUCKET"}
    assert required <= keys, sorted(required - keys)
    assert cron["startCommand"] == "python scripts/maintenance.py"
