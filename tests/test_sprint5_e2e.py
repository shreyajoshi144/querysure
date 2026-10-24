"""Sprint 5 end-to-end: a real headless Chromium drives the real UI against a live uvicorn server (stub LLM, SQLite, local
storage, dev mailbox, generic HMAC billing adapter). Runs the SAME flows against the unminified sources and the minified,
content-hashed bundle. Skipped when node / the npm-packaged Chromium are not installed (npm install)."""
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _have_browser_stack():
    if not shutil.which("node"):
        return False
    return all(os.path.isdir(os.path.join(ROOT, "node_modules", p)) for p in ("puppeteer-core", "@sparticuz/chromium"))


pytestmark = pytest.mark.skipif(not _have_browser_stack(), reason="needs node + `npm install` (puppeteer-core, @sparticuz/chromium)")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.4)
    return False


@pytest.mark.parametrize("minified", [False, True], ids=["source-assets", "minified-assets"])
def test_critical_user_flows_in_real_browser(tmp_path, minified):
    if not os.path.exists(os.path.join(ROOT, "retail.db")):
        subprocess.run([sys.executable, "create_retail.py"], cwd=ROOT, check=True, capture_output=True)
    dist = os.path.join(ROOT, "static", "dist")
    shutil.rmtree(dist, ignore_errors=True)
    if minified:
        subprocess.run([sys.executable, "scripts/build_static.py"], cwd=ROOT, check=True, capture_output=True)
    app_port, llm_port = _free_port(), _free_port()
    base = f"http://127.0.0.1:{app_port}"
    mail, dl = tmp_path / "mail", tmp_path / "dl"
    mail.mkdir(); dl.mkdir()
    env = dict(
        os.environ,
        ENVIRONMENT="development", JWT_SECRET_KEY="e2e-secret-" + "x" * 32, DATABASE_URL=f"sqlite:///{tmp_path}/e2e.db",
        EMAIL_PROVIDER="dev", DEV_EMAIL_DIR=str(mail), APP_BASE_URL=base,
        GROQ_API_KEY="stub-key", GROQ_BASE_URL=f"http://127.0.0.1:{llm_port}",
        PAYMENT_PROVIDER="generic", PAYMENT_WEBHOOK_SECRETS="e2e_webhook_secret_0123456789",
        PAYMENT_CHECKOUT_URL="https://pay.example.test/checkout", PAYMENT_CHECKOUT_SIGNING_SECRET="e2e_checkout_secret_0123456789",
        PAYMENT_PORTAL_URL="https://pay.example.test/portal",
        UPLOAD_DIR=str(tmp_path / "uploads"), LOCAL_REPORT_DIR=str(tmp_path / "reports"),
        E2E_BASE=base, E2E_MAIL_DIR=str(mail), E2E_WEBHOOK_SECRET="e2e_webhook_secret_0123456789",
        E2E_DOWNLOAD_DIR=str(dl), E2E_REPO=ROOT, E2E_PYTHON=sys.executable,
    )
    env.pop("REDIS_URL", None)
    procs = []
    try:
        procs.append(subprocess.Popen([sys.executable, "e2e/stub_llm.py", str(llm_port)], cwd=ROOT, env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        server_log = open(tmp_path / "server.log", "w")
        procs.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", str(app_port)],
                                      cwd=ROOT, env=env, stdout=server_log, stderr=subprocess.STDOUT))
        assert _wait_http(base + "/api/health"), (tmp_path / "server.log").read_text()[-2000:]
        run = subprocess.run(["node", "e2e/e2e.mjs"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=420)
        print(run.stdout[-6000:], run.stderr[-2000:])
        assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-2000:]
        # server log hygiene: no password, bearer token or one-time token ever reached the application/access log
        log = (tmp_path / "server.log").read_text(errors="replace")
        assert "E2eTestPassw0rd!" not in log and "WrongPassword123!" not in log and "Typed-Secret" not in log
        assert not re.search(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.", log), "a JWT appeared in the server log"
        assert not re.search(r"(verify_token|reset_token)=[A-Za-z0-9_-]{10,}", log), "a one-time token appeared in the server log"
        if minified:
            assert os.path.isdir(dist)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()
        shutil.rmtree(dist, ignore_errors=True)
