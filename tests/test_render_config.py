import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _render_env_keys():
    doc = yaml.safe_load((ROOT / "render.yaml").read_text())
    return {item["key"] for item in doc["services"][0]["envVars"] if "key" in item}


def test_production_required_env_vars_are_declared_in_render():
    required = {
        "JWT_SECRET_KEY",
        "EMAIL_PROVIDER",
        "SMTP_HOST",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "EMAIL_FROM",
        "APP_BASE_URL",
        "GOOGLE_CLIENT_ID",
        "DATABASE_URL",
        "REDIS_URL",
        "S3_BUCKET",
        "CORS_ALLOWED_ORIGINS",
    }
    missing = required - _render_env_keys()
    assert not missing, f"Missing production env vars in render.yaml: {sorted(missing)}"


def test_production_config_import_succeeds_with_render_contract():
    render_keys = _render_env_keys()
    env = {key: "" for key in render_keys}
    env.update({
        "ENVIRONMENT": "production",
        "JWT_SECRET_KEY": "test-production-secret",
        "EMAIL_PROVIDER": "smtp",
        "SMTP_HOST": "smtp.example.test",
        "SMTP_PORT": "587",
        "SMTP_USERNAME": "user",
        "SMTP_PASSWORD": "password",
        "EMAIL_FROM": "QuerySure <no-reply@example.test>",
        "APP_BASE_URL": "https://app.example.test",
        "GOOGLE_CLIENT_ID": "client-id",
        "DATABASE_URL": "postgresql+psycopg2://user:pass@localhost:5432/querysure",
        "REDIS_URL": "redis://localhost:6379/0",
        "S3_BUCKET": "querysure-test",
        "CORS_ALLOWED_ORIGINS": "https://app.example.test",
        "TRUSTED_PROXY_IPS": "10.0.0.0/8",
        "CSP_REPORT_ONLY": "0",
        "DAILY_LLM_BUDGET_USD": "2.00",
    })
    # Include only the variables represented by render.yaml plus PATH needed by Python.
    process_env = {"PATH": os.environ.get("PATH", "")}
    process_env.update(env)
    code = "import config; print(config.IS_PRODUCTION)"
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=process_env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_render_declares_background_worker_with_shared_runtime_contract():
    doc = yaml.safe_load((ROOT / "render.yaml").read_text())
    workers = [svc for svc in doc["services"] if svc.get("type") == "worker" and svc.get("name") == "querysure-worker"]
    assert len(workers) == 1
    worker = workers[0]
    assert worker["startCommand"] == "python worker.py"
    keys = {item["key"] for item in worker["envVars"] if "key" in item}
    for required in {"DATABASE_URL", "REDIS_URL", "S3_BUCKET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "JWT_SECRET_KEY"}:
        assert required in keys
