"""Sprint 4 closure: REAL LLM failure injection through the live app (real Postgres, Redis, SDK clients over HTTP to
loadtests/fake_llm_modes.py). One fresh uvicorn process per case (circuit breakers are per process).
    python loadtests/failure_injection.py            (Postgres + Redis running; see docs/03_EVALUATION.md)
For each case it records: HTTP result, stub call counts, user/global budget deltas, query-slot deltas, breaker/events, whether any
provider canary (key / e-mail embedded in the stub's error bodies) leaked into the response, the app log or the ops views."""
from __future__ import annotations

import json, os, subprocess, sys, time, uuid
import httpx, psycopg2, redis

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
BASE = "http://127.0.0.1:8000"
STUB = "http://127.0.0.1:5557"
LOG = "/tmp/qsload/inject_web.log"
CANARIES = ("CANARYPROVIDERKEY", "canary.victim@example.com", "sk-CANARY")
CSV = ("region,amount\n" + "\n".join(f"{r},{i}" for i in range(50) for r in ("N", "S"))).encode()
DB_URL = os.environ["DATABASE_URL"].replace("+psycopg2", "")
R = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)


def sql(q, args=()):
    with psycopg2.connect(DB_URL) as c, c.cursor() as cur:
        cur.execute(q, args)
        return cur.fetchall() if cur.description else None


def start_web(extra=None, workers=1):
    env = dict(os.environ, ENVIRONMENT="development", GROQ_API_KEY="fake-groq", OPENAI_API_KEY="fake-openai",
               GROQ_BASE_URL=STUB, OPENAI_BASE_URL=STUB + "/oa", AI_PROVIDER_TIMEOUT_SECONDS="2", AI_REQUEST_DEADLINE_SECONDS="14",
               CIRCUIT_FAILURE_THRESHOLD="5", CIRCUIT_COOLDOWN_SECONDS="4", GLOBAL_DAILY_AI_BUDGET_USD="25", LOG_LEVEL="INFO",
               ADMIN_EMAILS="admin@load.test", GAUGE_TTL_SECONDS="10")
    env.pop("STORAGE_BACKEND", None); env.pop("S3_BUCKET", None); env.pop("S3_ENDPOINT_URL", None)
    env.update(extra or {})
    open(LOG, "w").close()
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", "8000", "--workers", str(workers)],
                         cwd=ROOT, env=env, stdout=open(LOG, "a"), stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(60):
        try:
            if httpx.get(BASE + "/api/ready", timeout=2).status_code == 200:
                return p
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("web did not start")


def stop_web(p):
    import signal
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    p.wait(timeout=20); time.sleep(1)


def stub(path, body=None):
    return httpx.post(STUB + path, json=body or {}, timeout=5).json() if body is not None or path != "/_stats" else httpx.get(STUB + path).json()


def seed():
    from auth import create_access_token
    from db import SessionLocal, User, init_db
    init_db()
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        u = User(email=f"inj-{tag}@load.test", hashed_password="x", email_verified=True, plan="business")
        a = db.query(User).filter(User.email == "admin@load.test").first()
        if a is None:
            a = User(email="admin@load.test", hashed_password="x", email_verified=True, plan="business"); db.add(a)
        db.add(u); db.commit(); db.refresh(u); db.refresh(a)
        tok = lambda x: {"Authorization": "Bearer " + create_access_token(x.id, auth_version=int(x.auth_version or 0))}
        return u.id, tok(u), tok(a)


def new_workspace(h):
    c = httpx.Client(base_url=BASE, timeout=60)
    sid = c.post("/api/session", headers=h, json={}).json()["session_id"]
    r = c.post("/api/connect/upload", headers=h, data={"session_id": sid}, files={"file": ("sales.csv", CSV, "text/csv")})
    assert r.status_code in (200, 202), r.text
    for _ in range(40):
        row = sql("select schema is not null from pipeline_sessions where id=%s", (sid,))
        if row and row[0][0]:
            break
        time.sleep(0.25)
    return c, sid


def usage(uid):
    r = sql("select coalesce(sum(requests),0), coalesce(sum(estimated_cost_cents),0), coalesce(sum(query_count),0) from usage_daily where user_id=%s", (uid,))[0]
    keys = {k: R.get(k) for k in R.scan_iter("querysure:*") if time.strftime("%Y-%m-%d", time.gmtime()) in k or uid in k}
    return {"requests": int(r[0]), "cost_cents": round(float(r[1]), 5), "queries": int(r[2]), "redis": keys}


def generate(c, h, sid):
    t = time.time()
    r = c.post("/api/generate-sql", headers=h, json={"session_id": sid, "question": "total amount by region", "api_key": "user-key"})
    return r, round((time.time() - t) * 1000)


def leaks(*texts):
    blob = "\n".join(texts)
    return [c for c in CANARIES if c in blob]


def ops(admin):
    s = httpx.get(BASE + "/api/admin/ops/summary?window_minutes=5", headers=admin, timeout=30).json()
    ev = httpx.get(BASE + "/api/admin/ops/events?limit=100", headers=admin, timeout=30).text
    er = httpx.get(BASE + "/api/admin/ops/errors", headers=admin, timeout=30).text
    return s, ev, er


def case(name, modes, fn):
    for k in list(R.scan_iter("qs:m:*")) + list(R.scan_iter("qs:g:*")) + ["qs:err:meta", "qs:err:count"]:
        R.delete(k)                                   # per-case metrics: ops numbers below belong to THIS case only
    sql("delete from ops_events")
    stub("/_reset", {}); stub("/_mode", modes)
    p = start_web()
    try:
        uid, h, admin = seed(); c, sid = new_workspace(h)
        before = usage(uid)
        out = fn(c, h, admin, sid, uid)
        after = usage(uid)
        s, ev, er = ops(admin)
        log = open(LOG, errors="replace").read()
        res = {"case": name, "modes": modes, **out, "stub_calls": stub("/_stats")["calls"],
               "delta": {"requests": after["requests"] - before["requests"], "cost_cents": round(after["cost_cents"] - before["cost_cents"], 5),
                         "queries": after["queries"] - before["queries"],
                         "redis": {k: (before["redis"].get(k), v) for k, v in after["redis"].items() if before["redis"].get(k) != v}},
               "breakers": s["ai"]["circuit_breakers"], "ops_provider_failures": {p_: v["failure_reasons"] for p_, v in s["ai"]["providers"].items()},
               "ops_ai": {k: s["ai"][k] for k in ("requests", "success", "failure", "fallbacks", "deadline_exceeded")},
               "leaked_canaries": {"response": out.pop("_leak_resp", []), "log": leaks(log), "ops": leaks(json.dumps(s), ev, er)},
               "log_has_5xx_unhandled": "unhandled_exception" in log}
        return res
    finally:
        stop_web(p)


def one_generate(c, h, admin, sid, uid):
    r, ms = generate(c, h, sid)
    body = r.json()
    return {"status": r.status_code, "error_code": body.get("error_code"), "error": body.get("error"), "latency_ms": ms,
            "provider_used": body.get("provider_used"), "fallback_used": body.get("fallback_used"),
            "validation_valid": (body.get("validation") or {}).get("is_valid"), "_leak_resp": leaks(r.text)}


def repair_case(c, h, admin, sid, uid):
    out = one_generate(c, h, admin, sid, uid)
    r = c.post("/api/generate-sql", headers=h, json={"session_id": sid, "question": "again", "api_key": "k"})  # same shape, 2nd request
    ex = c.post("/api/execute-sql", headers=h, json={"session_id": sid, "sql": "SELECT n.nope FROM nonexistent_table AS n"})
    out["execute_invalid_sql_status"] = ex.status_code
    out["_leak_resp"] = leaks(ex.text) + out["_leak_resp"]
    return out


def slot_case(c, h, admin, sid, uid):
    good = "SELECT s.region, SUM(s.amount) AS total FROM sales AS s GROUP BY s.region"
    r1 = c.post("/api/execute-sql", headers=h, json={"session_id": sid, "sql": good})
    q1 = usage(uid)["queries"]
    path = sql("select db_path from pipeline_sessions where id=%s", (sid,))[0][0]
    os.rename(path, path + ".orig")
    open(path, "wb").write(b"this is not a sqlite database" * 50)   # present but corrupt: passes the pre-checks, fails INSIDE execution
    try:
        r2 = c.post("/api/execute-sql", headers=h, json={"session_id": sid, "sql": good})
    finally:
        os.replace(path + ".orig", path)
    q2 = usage(uid)["queries"]
    s, _, _ = ops(admin)
    return {"first_status": r1.status_code, "queries_after_success": q1, "failed_status": r2.status_code, "queries_after_failure": q2,
            "failed_body_error": r2.json().get("error"), "ops_query_slots": s["infrastructure"]["query_slots"], "_leak_resp": leaks(r2.text)}


def _delta(a, b):
    return {k: b.get(k, 0) - a.get(k, 0) for k in set(a) | set(b)}


def breaker_case(c, h, admin, sid, uid):
    seq = []
    for i in range(5):                                     # 5 failing requests: groq opens during #3, openai during #5
        r, ms = generate(c, h, sid); seq.append({"i": i, "status": r.status_code, "ms": ms})
    s_open, _, _ = ops(admin)
    open_states = dict(s_open["ai"]["circuit_breakers"])
    before = stub("/_stats")["calls"]
    r, ms = generate(c, h, sid)                           # 6th request: every breaker open -> must make NO network call
    skipped = _delta(before, stub("/_stats")["calls"])
    stub("/_mode", {"groq": "ok", "openai": "ok"}); time.sleep(5)       # cooldown (4s) elapses; providers healthy again
    r2, ms2 = generate(c, h, sid)                         # half-open probe -> success -> closed
    s2, _, _ = ops(admin)
    ev = sql("select kind, count(*) from ops_events where kind like 'circuit_%%' group by kind")
    return {"status": r.status_code, "failures_seq": seq, "calls_made_while_all_open": skipped, "states_when_open": open_states,
            "recovery_status": r2.status_code, "states_after_recovery": s2["ai"]["circuit_breakers"], "circuit_events": dict(ev),
            "latency_ms": ms, "error_code": r.json().get("error_code"), "_leak_resp": leaks(r.text, r2.text)}


CASES = [
    ("A groq primary 500 -> groq fallback model ok", {"groq:" + os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"): "500"}, one_generate),
    ("B groq 429 (both models) -> openai ok", {"groq": "429"}, one_generate),
    ("C groq timeout (both models) -> openai ok", {"groq": "timeout"}, one_generate),
    ("D malformed output from every provider", {"groq": "malformed", "openai": "malformed"}, one_generate),
    ("E repair failure (bad SQL, every repair attempt malformed)", {"groq": "badsql_then_garbage", "groq:llama-3.1-8b-instant": "malformed", "openai": "malformed"}, repair_case),
    ("F all providers 5xx", {"groq": "500", "openai": "500"}, one_generate),
    ("G all providers timeout (deadline)", {"groq": "timeout", "openai": "timeout"}, one_generate),
    ("H breaker open -> skip -> half-open recovery", {"groq": "500", "openai": "500"}, breaker_case),
    ("I query-slot refund when execution fails", {}, slot_case),
]

if __name__ == "__main__":
    only = sys.argv[1:]
    results = []
    for name, modes, fn in CASES:
        if only and name[0] not in only:
            continue
        print("running", name, flush=True)
        try:
            res = case(name, modes, fn)
        except Exception as e:  # noqa: BLE001
            res = {"case": name, "harness_error": f"{type(e).__name__}: {e}"}
        results.append(res)
        print(json.dumps({k: res.get(k) for k in ("case", "status", "error_code", "provider_used", "fallback_used", "latency_ms", "delta", "leaked_canaries", "harness_error")}, default=str)[:900], flush=True)
    os.makedirs(os.path.join(ROOT, "loadtests/results"), exist_ok=True)
    json.dump(results, open(os.path.join(ROOT, "loadtests/results/failure_injection.json"), "w"), indent=2, default=str)
