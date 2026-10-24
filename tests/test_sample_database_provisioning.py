"""
IMP-6: /api/connect/sample returned 400 on every fresh deploy, because
retail.db isn't (and shouldn't be) committed to the repo, and nothing in the
Docker build or Render's buildCommand ever generated it -- only start.py's
local-dev launcher did (ensure_sample_db()). These tests check the fix at
three levels: the generator itself produces a real, usable database; the
deploy configs actually invoke it, in the right order; and the resulting
file works through the real HTTP endpoint end to end.
"""
from __future__ import annotations

from tests.auth_test_utils import verify_registered_email

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

import pytest
import yaml

import api as api_module

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _generate_in(tmpdir: str) -> str:
    """Runs the real create_retail.py, unmodified, in its own directory --
    exactly what the Docker build step and Render's buildCommand do."""
    shutil.copy(os.path.join(REPO_ROOT, "create_retail.py"), tmpdir)
    result = subprocess.run(
        [sys.executable, "create_retail.py"], cwd=tmpdir, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"create_retail.py failed:\n{result.stdout}\n{result.stderr}"
    path = os.path.join(tmpdir, "retail.db")
    assert os.path.exists(path)
    return path


# ===================================================================
# The generator itself
# ===================================================================

def test_create_retail_produces_a_real_usable_database(tmp_path):
    path = _generate_in(str(tmp_path))
    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"stores", "products", "customers", "orders", "order_items"} <= tables
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] > 0
    conn.close()


def test_create_retail_is_deterministic_across_builds(tmp_path):
    """A build step should be reproducible. This also guards against someone
    later removing create_retail.py's fixed random seed without noticing --
    that would make every deploy ship a different 'sample' dataset."""
    d1, d2 = tmp_path / "build1", tmp_path / "build2"
    d1.mkdir()
    d2.mkdir()
    p1, p2 = _generate_in(str(d1)), _generate_in(str(d2))

    def sha256(path):
        return hashlib.sha256(open(path, "rb").read()).hexdigest()

    assert sha256(p1) == sha256(p2)


# ===================================================================
# The deploy configs actually invoke it (and in the right order)
# ===================================================================

def test_dockerfile_generates_sample_db_after_copying_source():
    dockerfile = open(os.path.join(REPO_ROOT, "Dockerfile")).read()
    assert "RUN python create_retail.py" in dockerfile
    # create_retail.py must exist in the image (i.e. the source has been
    # COPYed in) before this RUN step executes, or the build fails.
    assert dockerfile.index("COPY . .") < dockerfile.index("RUN python create_retail.py")


def test_render_build_command_generates_sample_db_after_installing_deps():
    with open(os.path.join(REPO_ROOT, "render.yaml")) as f:
        config = yaml.safe_load(f)
    build_command = next(s["buildCommand"] for s in config["services"] if s["name"] == "querysure-api")
    assert "python create_retail.py" in build_command
    assert build_command.index("pip install") < build_command.index("python create_retail.py")


# ===================================================================
# End to end: the artifact a real build produces is actually usable
# ===================================================================

def _register(client):
    import uuid
    email = f"sample-{uuid.uuid4().hex[:10]}@example.com"
    pw = "TestPassword123!"
    assert client.post("/api/auth/register", json={"email": email, "password": pw}).status_code == 200
    verify_registered_email(client, email)
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_connect_sample_works_against_a_freshly_built_database(client, tmp_path, monkeypatch):
    """This is the exact failure the bug caused: a fresh deploy, with only
    what the build step produces -- no pre-existing retail.db, no manual
    `python create_retail.py` run by a human after the fact."""
    fresh_db = _generate_in(str(tmp_path))
    monkeypatch.setattr(api_module, "SAMPLE_DB", fresh_db)

    headers = _register(client)
    r = client.post("/api/connect/sample", headers=headers, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["table_count"] == 5
    assert body["total_rows"] > 0

    ex = client.post("/api/execute-sql", headers=headers,
                     json={"session_id": body["session_id"], "sql": "SELECT COUNT(*) AS n FROM orders"})
    assert ex.status_code == 200, ex.text
    assert ex.json()["rows"][0]["n"] > 0


def test_connect_sample_400s_clearly_when_no_database_exists_at_all(client, monkeypatch):
    """Pins the OLD (still-correct) behavior for the case the fix doesn't
    touch: if provisioning is skipped entirely, this stays a clean 400 with
    an actionable message, not a crash."""
    monkeypatch.setattr(api_module, "SAMPLE_DB", "/nonexistent/retail.db")
    headers = _register(client)
    r = client.post("/api/connect/sample", headers=headers, json={})
    assert r.status_code == 400
    body = r.json()
    assert body["success"] is False
    assert "create_retail.py" in body["error"]
