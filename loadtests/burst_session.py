"""Isolated burst: N distinct users call POST /api/session at the same instant (nothing else running).
    python loadtests/burst_session.py N     (needs DATABASE_URL etc. like sprint4_load.py)"""
import asyncio, json, os, sys, time, uuid
import httpx
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from loadtests.sprint4_load import seed_users, pct

async def main(n):
    users, _ = seed_users(n, "admin@load.test")
    async with httpx.AsyncClient(base_url=os.environ.get("LOAD_BASE_URL", "http://127.0.0.1:8000"), timeout=90) as c:
        async def one(u):
            t = time.perf_counter()
            r = await c.post("/api/session", headers={"Authorization": f"Bearer {u['token']}"}, json={})
            return r.status_code, (time.perf_counter() - t) * 1000
        t0 = time.perf_counter()
        res = await asyncio.gather(*[one(u) for u in users])
    lat = [x[1] for x in res]; codes = {}
    for s, _ in res: codes[s] = codes.get(s, 0) + 1
    print(json.dumps({"users": n, "codes": codes, "p50_ms": pct(lat, .5), "p95_ms": pct(lat, .95), "max_ms": round(max(lat)), "wall_s": round(time.perf_counter() - t0, 1)}))
asyncio.run(main(int(sys.argv[1])))
