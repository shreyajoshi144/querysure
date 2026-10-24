"""Sprint 4 closure: production alert configuration + REAL alert delivery, and Sentry transport scrubbing.
  A. config guards under ENVIRONMENT=production (http webhook refused, https accepted, defaults)
  B. live stack in production mode (2 web processes + RQ worker + moto S3 + Postgres + Redis) with ALERT_WEBHOOK_URL pointing at a
     local HTTPS receiver (self-signed cert trusted via SSL_CERT_FILE): healthy baseline -> no alerts; provider outage -> firing
     (exactly once across 2 processes); recovery -> resolved; Redis outage -> critical alert + resolve; receiver down -> delivery
     failure is counted, incident still recorded, app keeps serving; payloads contain no user data.
  C. Sentry SDK: real transport to a local HTTP receiver, envelope inspected for leaked content.
Not verifiable here: a real Sentry project / Slack / PagerDuty endpoint (no credentials, no outbound internet)."""
import gzip, json, os, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
import httpx, redis

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "loadtests/results/alert_delivery.json")
HOOKS = "/tmp/qsload/hooks.jsonl"
CERT, KEY = "/tmp/qsload/hook.crt", "/tmp/qsload/hook.key"
result = {}


def run_py(env_extra, code):
    env = {"PATH": os.environ["PATH"], "JWT_SECRET_KEY": "k" * 40, "PYTHONPATH": ROOT}
    env.update(env_extra)
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip().splitlines()[-1][:200] if (p.stdout + p.stderr).strip() else ""


PROD = {"ENVIRONMENT": "production", "DATABASE_URL": "postgresql+psycopg2://x:y@127.0.0.1/z", "REDIS_URL": "redis://127.0.0.1:6379/0",
        "TRUSTED_PROXY_IPS": "127.0.0.1", "CORS_ALLOWED_ORIGINS": "https://app.test", "STORAGE_BACKEND": "s3", "S3_BUCKET": "b",
        "SMTP_HOST": "h", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "p", "EMAIL_FROM": "a@b.co", "APP_BASE_URL": "https://app.test", "GOOGLE_CLIENT_ID": "x.apps.googleusercontent.com"}


def part_a():
    a = {}
    a["http_webhook_refused"] = run_py({**PROD, "ALERT_WEBHOOK_URL": "http://hooks.example.com/x"}, "import config")
    a["https_webhook_accepted"] = run_py({**PROD, "ALERT_WEBHOOK_URL": "https://hooks.example.com/x"}, "import config; print('ok', config.ALERTS_ENABLED)")
    a["alerts_default_in_production"] = run_py({**PROD}, "import config; print('ALERTS_ENABLED=', config.ALERTS_ENABLED, 'webhook=', repr(config.ALERT_WEBHOOK_URL))")
    a["alerts_default_in_development"] = run_py({"ENVIRONMENT": "development"}, "import config; print('ALERTS_ENABLED=', config.ALERTS_ENABLED)")
    a["admin_emails_default_empty"] = run_py({**PROD}, "import config; print(sorted(config.ADMIN_EMAILS))")
    a["sentry_dsn_without_sdk_installed_is_safe"] = run_py({**PROD, "SENTRY_DSN": "https://k@o0.ingest.sentry.io/1"},
        "import sys; sys.modules['sentry_sdk']=None\nfrom observability import errors; print('init ->', errors.init_sentry())")
    return a


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        open(HOOKS, "ab").write(body + b"\n")
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass


def start_receiver():
    import ssl
    srv = HTTPServer(("127.0.0.1", 5558), Hook)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(CERT, KEY)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def hooks():
    return [json.loads(l) for l in open(HOOKS).read().splitlines() if l.strip()] if os.path.exists(HOOKS) else []


def summarize(h):
    return [(x["status"], x["alert"], x["severity"]) for x in h]


def part_b():
    from loadtests.failure_injection import start_web, stop_web, seed, new_workspace, generate, stub, BASE, sql
    b = {}
    subprocess.run(["bash", "-c", f"openssl req -x509 -newkey rsa:2048 -nodes -keyout {KEY} -out {CERT} -days 1 -subj /CN=127.0.0.1 "
                    f"-addext 'subjectAltName=IP:127.0.0.1' >/dev/null 2>&1"], check=True)
    open(HOOKS, "w").close()
    recv = start_receiver()
    env = dict(ENVIRONMENT="production", STORAGE_BACKEND="s3", S3_BUCKET="qs-load", S3_ENDPOINT_URL="http://127.0.0.1:5555", S3_REGION="us-east-1",
               AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test", AWS_DEFAULT_REGION="us-east-1", TRUSTED_PROXY_IPS="127.0.0.1",
               CORS_ALLOWED_ORIGINS="https://app.test", SMTP_HOST="127.0.0.1", SMTP_USERNAME="u", SMTP_PASSWORD="p", EMAIL_FROM="a@b.co",
               APP_BASE_URL="https://app.test", GOOGLE_CLIENT_ID="x.apps.googleusercontent.com", ALERT_WEBHOOK_URL="https://127.0.0.1:5558/hook", SSL_CERT_FILE=CERT,
               ALERT_EVAL_INTERVAL_SECONDS="5", ALERT_RESOLVE_AFTER_SECONDS="5", OPS_METRICS_TOKEN="scrape-token-123456",
               CIRCUIT_COOLDOWN_SECONDS=os.environ.get("ALERT_TEST_COOLDOWN", "4"), GAUGE_TTL_SECONDS="10")
    stub("/_reset", {}); stub("/_mode", {})
    worker = None
    web = start_web(env, workers=2)
    wenv = dict(os.environ, **{**env, "GROQ_API_KEY": "fake-groq", "OPENAI_API_KEY": "fake-openai", "GROQ_BASE_URL": "http://127.0.0.1:5557",
                               "OPENAI_BASE_URL": "http://127.0.0.1:5557/oa", "REDIS_URL": os.environ["REDIS_URL"]})
    worker = subprocess.Popen([sys.executable, "worker.py"], cwd=ROOT, env=wenv, stdout=open("/tmp/qsload/alert_worker.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
    try:
        uid, h, admin = seed()
        c, sid = new_workspace(h)
        b["ready_in_production"] = httpx.get(BASE + "/api/ready").json()
        time.sleep(14)
        b["baseline_alerts_after_14s_healthy"] = summarize(hooks())
        # --- provider outage
        stub("/_mode", {"groq": "500", "openai": "500"})
        codes = []
        for _ in range(12):
            r, _ms = generate(c, h, sid); codes.append(r.status_code)
        b["outage_request_statuses"] = sorted(set(codes))
        time.sleep(18)
        fired = hooks(); b["after_outage"] = summarize(fired)
        # --- recovery
        stub("/_mode", {"groq": "ok", "openai": "ok"}); time.sleep(5)
        for _ in range(8):
            generate(c, h, sid); time.sleep(0.5)
        time.sleep(25)
        b["after_recovery"] = summarize(hooks())
        inc = httpx.get(BASE + "/api/admin/ops/incidents?limit=50", headers=admin, timeout=30).json()["incidents"]
        b["incidents"] = [(i["alert"], i["key"], i["status"], i["occurrences"]) for i in inc]
        # --- redis outage
        before = len(hooks())
        subprocess.run(["redis-cli", "shutdown", "nosave"], capture_output=True); time.sleep(16)
        mid = summarize(hooks()[before:])
        subprocess.run(["bash", "/home/claude/svc.sh"], capture_output=True); time.sleep(30)
        b["redis_outage_alerts"] = mid
        b["after_redis_restore"] = summarize(hooks()[before:])
        b["ready_after_redis_restore"] = httpx.get(BASE + "/api/ready").json()
        # --- receiver down: delivery fails, incident recorded, app keeps serving
        recv.shutdown(); recv.server_close(); n_before = len(hooks())
        stub("/_mode", {"groq": "500", "openai": "500"})
        for _ in range(12):
            r, _ms = generate(c, h, sid)
        time.sleep(18)
        prom = httpx.get(BASE + "/api/admin/ops/metrics", headers={"X-Ops-Token": "scrape-token-123456"}, timeout=30).text
        b["receiver_down"] = {"new_deliveries": len(hooks()) - n_before,
                              "alert_delivery_metric": [l for l in prom.splitlines() if l.startswith("qs_alert_delivery_window")],
                              "app_still_serves": httpx.get(BASE + "/api/auth/me", headers=h, timeout=30).status_code,
                              "incident_rows_open": [(i["alert"], i["status"]) for i in httpx.get(BASE + "/api/admin/ops/incidents?status=open", headers=admin, timeout=30).json()["incidents"]]}
        # --- payload privacy + dedupe
        allh = hooks()
        blob = json.dumps(allh)
        b["payload_leaks"] = [x for x in (uid, "inj-", "@load.test", "total amount by region", "fake-groq", "user-key") if x in blob]
        keys = {}
        for x in allh:
            keys[(x["status"], x["alert"])] = keys.get((x["status"], x["alert"]), 0) + 1
        b["deliveries_per_(status,alert)"] = {f"{k[0]}:{k[1]}": v for k, v in keys.items()}
        b["sample_payload"] = allh[0] if allh else None
    finally:
        for p in (worker, web):
            if p:
                stop_web(p)
        try:
            recv.shutdown()
        except Exception:
            pass
    return b


class Sink(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        if self.headers.get("content-encoding") == "gzip":
            body = gzip.decompress(body)
        open("/tmp/qsload/sentry_envelopes.bin", "ab").write(body + b"\n---\n")
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass


def part_c():
    open("/tmp/qsload/sentry_envelopes.bin", "wb").close()
    srv = HTTPServer(("127.0.0.1", 5559), Sink); threading.Thread(target=srv.serve_forever, daemon=True).start()
    code = r'''
import os, sys
sys.path.insert(0, ".")
from fastapi.testclient import TestClient
import api, config
from observability import errors
assert errors.init_sentry() is True
async def boom():
    raise RuntimeError("leak victim.person@example.com password=hunter2 SELECT * FROM secrets")
api.app.add_api_route("/api/_sentry_boom", boom, methods=["POST"])
with TestClient(api.app, raise_server_exceptions=False) as c:
    r = c.post("/api/_sentry_boom?token=QUERYSECRET1234567890", json={"question": "CONFIDENTIAL-QUESTION-TEXT", "sql": "select ssn from payroll"},
               headers={"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ2aWN0aW0ifQ.sig12345678901", "X-Api-Key": "sk-LEAKYAPIKEY0123456789abcdef"})
    print("status", r.status_code)
import sentry_sdk; sentry_sdk.flush(5)
'''
    env = dict(os.environ, ENVIRONMENT="development", SENTRY_DSN="http://publickey@127.0.0.1:5559/1", JWT_SECRET_KEY="k" * 40, DATABASE_URL="sqlite:////tmp/qsload/sentry_c.db")
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    time.sleep(1); srv.shutdown()
    raw = open("/tmp/qsload/sentry_envelopes.bin", errors="replace").read()
    forbidden = ["victim.person@example.com", "hunter2", "SELECT * FROM secrets", "QUERYSECRET", "CONFIDENTIAL-QUESTION", "select ssn", "eyJhbGci", "LEAKYAPIKEY", "publickey"]
    events = [json.loads(l) for chunk in raw.split("\n---\n") for l in chunk.splitlines() if l.startswith("{") and '"exception"' in l]
    return {"app_exit": p.returncode, "app_tail": (p.stdout + p.stderr).strip().splitlines()[-1][:200], "envelopes_received": raw.count("---"),
            "forbidden_strings_found": [f for f in forbidden if f in raw],
            "event_top_level_keys": sorted(events[0]) if events else None,
            "exception_type_and_value": [(e.get("type"), e.get("value")) for e in (events[0].get("exception", {}).get("values", []) if events else [])],
            "tags": events[0].get("tags") if events else None}


if __name__ == "__main__":
    which = sys.argv[1:] or ["A", "B", "C"]
    if "A" in which: result["A_config_guards"] = part_a(); print(json.dumps(result["A_config_guards"], indent=1))
    if "C" in which: result["C_sentry_transport"] = part_c(); print(json.dumps(result["C_sentry_transport"], indent=1))
    if "B" in which: result["B_live_alert_delivery"] = part_b(); print(json.dumps({k: v for k, v in result["B_live_alert_delivery"].items() if k != "sample_payload"}, indent=1, default=str))
    old = json.load(open(OUT)) if os.path.exists(OUT) else {}
    old.update(result); json.dump(old, open(OUT, "w"), indent=1, default=str)
