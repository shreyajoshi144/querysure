"""Sprint 4 closure: /api/ready and /api/health while the DB pool is genuinely exhausted.
Method: 1 uvicorn process with a tiny pool (DB_POOL_SIZE=2, DB_MAX_OVERFLOW=0, default DB_POOL_TIMEOUT=10). A psql-style session holds
the user's row lock; POST /api/session needs that lock (quota check), so N concurrent calls each pin one pooled connection while
waiting. Then we probe readiness/liveness repeatedly and also try an unrelated authenticated request."""
import json, os, sys, threading, time
import httpx, psycopg2
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from loadtests.failure_injection import start_web, stop_web, seed, DB_URL, BASE

def main():
    p = start_web({"DB_POOL_SIZE": "2", "DB_MAX_OVERFLOW": "0", "ALERTS_ENABLED": "0"})
    try:
        uid, h, admin = seed()
        _, h2, _ = seed()
        holder = psycopg2.connect(DB_URL); cur = holder.cursor()
        cur.execute("select id from users where id=%s for update", (uid,))     # row lock held until we roll back
        blocked = []
        def blocker():
            t = time.time()
            try:
                r = httpx.post(BASE + "/api/session", headers=h, json={}, timeout=60); blocked.append((r.status_code, round(time.time() - t, 1)))
            except Exception as e:
                blocked.append((type(e).__name__, round(time.time() - t, 1)))
        ts = [threading.Thread(target=blocker) for _ in range(4)]
        [t.start() for t in ts]; time.sleep(1.5)
        out = {"probes": []}
        for i in range(4):
            for path in ("/api/health", "/api/ready"):
                t = time.time()
                try:
                    r = httpx.get(BASE + path, timeout=30); body = r.json(); code = r.status_code
                except Exception as e:
                    body, code = {"exc": type(e).__name__}, None
                out["probes"].append({"path": path, "status": code, "ms": round((time.time() - t) * 1000), "body": body, "t": round(time.time() % 1000, 1)})
            time.sleep(0.5)
        t = time.time()
        try:
            r = httpx.get(BASE + "/api/auth/me", headers=h2, timeout=30); other = (r.status_code, round((time.time() - t) * 1000), r.json().get("error_code"))
        except Exception as e:
            other = (type(e).__name__, round((time.time() - t) * 1000), None)
        out["unrelated_authenticated_request_during_exhaustion"] = other
        holder.rollback(); holder.close(); [t.join() for t in ts]
        out["blocked_requests"] = blocked
        time.sleep(1)
        rec = [httpx.get(BASE + "/api/ready", timeout=30) for _ in range(2)]
        out["recovered_ready"] = [(r.status_code, r.json()["status"]) for r in rec]
        print(json.dumps(out, indent=1))
        json.dump(out, open(os.path.join(os.path.dirname(__file__), "results", sys.argv[1] if len(sys.argv) > 1 else "pool_exhaustion.json"), "w"), indent=1)
    finally:
        stop_web(p)

main()
