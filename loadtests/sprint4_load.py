"""Sprint 4 load test driver: real Postgres + Redis + RQ worker + S3 (moto) + stub LLM, real HTTP to uvicorn.

Each virtual user: has its own pre-seeded verified account (JWT minted directly, so per-IP auth throttles do not dominate the
run; auth endpoints are exercised by the functional suite), uploads a CSV (-> RQ job, polled to completion), then loops a
weighted mix of: list sessions + auth/me (reads), generate-sql (LLM stub), execute-sql, re-upload (RQ job).
Per phase it records client-side latency per operation, status codes, throughput, upload->ready job latency, and samples
saturation (Postgres connections, Redis ops/clients/memory, RQ depth, per-process CPU/RSS, DB-pool utilisation reported by the
app itself) once a second. Server-side numbers come from the app's own /api/admin/ops/summary.

    python loadtests/sprint4_load.py --users 10,25,50,100,200 --duration 45 --think 1.0 --out loadtests/results
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict

import httpx
import psutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE = os.environ.get("LOAD_BASE_URL", "http://127.0.0.1:8000")
CSV = ("region,amount\n" + "\n".join(f"{r},{i}" for i in range(200) for r in ("N", "S", "E", "W"))).encode()


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(q * len(s)))], 1)


def summarize(values):
    return {"n": len(values), "p50_ms": pct(values, .50), "p95_ms": pct(values, .95), "p99_ms": pct(values, .99),
            "max_ms": round(max(values), 1) if values else None, "mean_ms": round(statistics.fmean(values), 1) if values else None}


# ---- seeding -------------------------------------------------------------------------------------------------------
def seed_users(n: int, admin_email: str):
    from auth import create_access_token
    from db import SessionLocal, User, init_db
    init_db()
    tag = uuid.uuid4().hex[:6]
    out = []
    with SessionLocal() as db:
        for i in range(n):
            u = User(email=f"load-{tag}-{i}@load.test", hashed_password="x", email_verified=True, plan="business")
            db.add(u)
        admin = db.query(User).filter(User.email == admin_email).first()
        if admin is None:
            admin = User(email=admin_email, hashed_password="x", email_verified=True, plan="business"); db.add(admin)
        db.commit()
        for u in db.query(User).filter(User.email.like(f"load-{tag}-%")).all():
            out.append({"id": u.id, "token": create_access_token(u.id, auth_version=int(u.auth_version or 0))})
        admin_token = create_access_token(admin.id, auth_version=int(admin.auth_version or 0))
    return out, admin_token


# ---- sampler -------------------------------------------------------------------------------------------------------
class Sampler(threading.Thread):
    def __init__(self, redis_url, pg_dsn):
        super().__init__(daemon=True)
        self.stop_ev = threading.Event()
        self.rows = []
        self.redis_url, self.pg_dsn = redis_url, pg_dsn

    def run(self):
        import psycopg2
        import redis
        r = redis.Redis.from_url(self.redis_url)
        pg = psycopg2.connect(self.pg_dsn); pg.autocommit = True
        cur = pg.cursor()
        procs = {}
        while not self.stop_ev.wait(1.0):
            row = {"t": time.time()}
            try:
                cur.execute("select count(*), count(*) filter (where state='active') from pg_stat_activity where datname=current_database()")
                row["pg_conns"], row["pg_active"] = cur.fetchone()
                info = r.info()
                row.update(redis_ops=info["instantaneous_ops_per_sec"], redis_clients=info["connected_clients"],
                           redis_mem_mb=round(info["used_memory"] / 1048576, 2), redis_blocked=info["blocked_clients"])
                from rq import Queue
                row["queue_depth"] = Queue("querysure", connection=r).count
                cpu, rss = 0.0, 0.0
                for p in psutil.process_iter(["pid", "cmdline"]):
                    cmd = " ".join(p.info["cmdline"] or [])
                    if "uvicorn api:app" in cmd or "multiprocessing" in cmd and "spawn" in cmd or "worker.py" in cmd or "-m rq" in cmd or "postgres:" in cmd or "redis-server" in cmd:
                        pr = procs.setdefault(p.pid, p)
                        cpu += pr.cpu_percent(None); rss += pr.memory_info().rss
                row["stack_cpu_pct"], row["stack_rss_mb"] = round(cpu, 1), round(rss / 1048576, 1)
                row["host_cpu_pct"] = psutil.cpu_percent(None)
            except Exception as e:  # noqa: BLE001
                row["err"] = type(e).__name__
            self.rows.append(row)

    def stop(self):
        self.stop_ev.set(); self.join(3)


# ---- virtual user --------------------------------------------------------------------------------------------------
class Stats:
    def __init__(self):
        self.lat = defaultdict(list)
        self.status = defaultdict(Counter)
        self.job_e2e = []
        self.errors = Counter()
        self.total = 0


async def timed(client, stats, op, method, url, **kw):
    t0 = time.perf_counter()
    try:
        r = await client.request(method, url, **kw)
        ms = (time.perf_counter() - t0) * 1000
        stats.lat[op].append(ms); stats.status[op][r.status_code] += 1; stats.total += 1
        return r
    except Exception as e:  # noqa: BLE001
        stats.errors[f"{op}:{type(e).__name__}"] += 1; stats.total += 1
        return None


async def upload_and_wait(client, stats, headers, sid, deadline):
    t0 = time.perf_counter()
    r = await timed(client, stats, "upload", "POST", "/api/connect/upload", headers=headers, data={"session_id": sid},
                    files={"file": ("sales.csv", CSV, "text/csv")})
    if r is None or r.status_code != 202:
        return False
    job_id = r.json().get("job_id")
    while time.perf_counter() < deadline:
        await asyncio.sleep(0.25)
        j = await timed(client, stats, "job_poll", "GET", f"/api/jobs/{job_id}", headers=headers)
        if j is not None and j.status_code == 200 and j.json().get("status") in ("succeeded", "failed"):
            stats.job_e2e.append((time.perf_counter() - t0) * 1000)
            return j.json().get("status") == "succeeded"
    stats.errors["job_timeout"] += 1
    return False


async def user_loop(user, stats, stop_at, think, rng):
    h = {"Authorization": f"Bearer {user['token']}"}
    async with httpx.AsyncClient(base_url=BASE, timeout=60.0, headers={}, limits=httpx.Limits(max_connections=4)) as c:
        r = await timed(c, stats, "create_session", "POST", "/api/session", headers=h, json={})
        if r is None or r.status_code != 200:
            return
        sid = r.json()["session_id"]
        ready = await upload_and_wait(c, stats, h, sid, stop_at + 60)
        n = 0
        while time.perf_counter() < stop_at:
            await asyncio.sleep(rng.uniform(0.5, 1.5) * think)
            n += 1
            roll = rng.random()
            if roll < 0.40:
                await timed(c, stats, "list_sessions", "GET", "/api/sessions", headers=h)
            elif roll < 0.55:
                await timed(c, stats, "auth_me", "GET", "/api/auth/me", headers=h)
            elif roll < 0.80 and ready:
                await timed(c, stats, "generate_sql", "POST", "/api/generate-sql", headers=h,
                            json={"session_id": sid, "question": "total amount by region", "api_key": "load-key"})
            elif roll < 0.95 and ready:
                await timed(c, stats, "execute_sql", "POST", "/api/execute-sql", headers=h,
                            json={"session_id": sid, "sql": "SELECT s.region, SUM(s.amount) AS total FROM sales AS s GROUP BY s.region"})
            elif ready:
                await upload_and_wait(c, stats, h, sid, stop_at + 60)


async def run_phase(users_all, admin_token, n_users, duration, think, seed):
    rng = random.Random(seed)
    users = users_all[:n_users]
    stats = Stats()
    stop_at = time.perf_counter() + duration
    t_start = time.time()
    tasks = [asyncio.create_task(user_loop(u, stats, stop_at, think, random.Random(rng.random()))) for u in users]
    await asyncio.gather(*tasks)
    wall = time.time() - t_start
    async with httpx.AsyncClient(base_url=BASE, timeout=30.0) as c:
        w = max(1, int(wall // 60) + 2)
        srv = (await c.get(f"/api/admin/ops/summary?window_minutes={w}", headers={"Authorization": f"Bearer {admin_token}"})).json()
    return stats, wall, srv


def phase_report(n_users, stats, wall, srv, samples):
    ok2xx = sum(c for op in stats.status.values() for code, c in op.items() if code < 300)
    by_code = Counter()
    for op in stats.status.values():
        by_code.update(op)
    server_5xx = sum(c for code, c in by_code.items() if code >= 500)
    rate_limited = by_code.get(429, 0)
    busy_503 = by_code.get(503, 0)
    rows = [s for s in samples if "err" not in s]
    def mx(k):
        return max((s[k] for s in rows if k in s), default=None)
    def avg(k):
        v = [s[k] for s in rows if k in s]; return round(statistics.fmean(v), 1) if v else None
    pool_peak = srv["infrastructure"]["database"]["pool"]
    return {
        "users": n_users, "wall_s": round(wall, 1), "requests": stats.total, "throughput_rps": round(stats.total / wall, 2),
        "success_2xx": ok2xx, "http_status": {str(k): v for k, v in sorted(by_code.items())},
        "server_errors_5xx": server_5xx, "rate_limited_429": rate_limited, "db_busy_503": busy_503,
        "client_exceptions": dict(stats.errors),
        "client_latency_ms": {op: summarize(v) for op, v in sorted(stats.lat.items())},
        "job_upload_to_ready_ms": summarize(stats.job_e2e),
        "server_view": {"http": srv["traffic"]["latency"], "5xx_rate_pct": srv["traffic"]["error_rate_5xx_pct"],
                       "queue_wait": srv["jobs"]["queue_wait"], "job_duration": srv["jobs"]["duration"],
                       "ai": {"requests": srv["ai"]["requests"], "latency": srv["ai"]["latency"], "tokens": srv["ai"]["tokens"], "cost_usd": srv["ai"]["cost_usd"]},
                       "slo_breaches": srv["slo"]["breaches"], "degraded": srv["degraded"], "pool_at_end": pool_peak},
        "saturation": {"pg_conns_max": mx("pg_conns"), "pg_active_max": mx("pg_active"), "redis_ops_max": mx("redis_ops"),
                       "redis_clients_max": mx("redis_clients"), "redis_mem_mb_max": mx("redis_mem_mb"), "queue_depth_max": mx("queue_depth"),
                       "stack_cpu_pct_avg": avg("stack_cpu_pct"), "host_cpu_pct_avg": avg("host_cpu_pct"), "host_cpu_pct_max": mx("host_cpu_pct"),
                       "stack_rss_mb_max": mx("stack_rss_mb")},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", default="10,25,50,100,200")
    ap.add_argument("--duration", type=int, default=45)
    ap.add_argument("--think", type=float, default=1.0)
    ap.add_argument("--out", default="loadtests/results")
    ap.add_argument("--label", default="baseline")
    a = ap.parse_args()
    levels = [int(x) for x in a.users.split(",")]
    admin_email = os.environ.get("ADMIN_EMAILS", "admin@load.test").split(",")[0]
    users_all, admin_token = seed_users(max(levels), admin_email)
    os.makedirs(a.out, exist_ok=True)
    results = []
    for n in levels:
        sampler = Sampler(os.environ["REDIS_URL"], os.environ["DATABASE_URL"].replace("+psycopg2", ""))
        psutil.cpu_percent(None); sampler.start()
        stats, wall, srv = asyncio.run(run_phase(users_all, admin_token, n, a.duration, a.think, seed=n))
        sampler.stop()
        rep = phase_report(n, stats, wall, srv, sampler.rows)
        results.append(rep)
        print(json.dumps({k: rep[k] for k in ("users", "requests", "throughput_rps", "http_status", "client_exceptions")}), flush=True)
        time.sleep(5)          # let the queue drain between phases
    path = os.path.join(a.out, f"load_{a.label}.json")
    json.dump({"label": a.label, "duration_s": a.duration, "think_s": a.think, "cpu_count": os.cpu_count(), "phases": results}, open(path, "w"), indent=2)
    print("wrote", path)


if __name__ == "__main__":
    main()
