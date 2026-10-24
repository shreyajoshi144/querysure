# Load tests

## Sprint 4: real Postgres + Redis/RQ stack (`sprint4_load.py`)
Drives the real HTTP API with N virtual users against: uvicorn (2 worker processes), a real RQ worker, Postgres, Redis, S3 (moto server) and a stub LLM (`fake_llm.py`, ~400 ms provider latency, schema-valid answer).
```
# Postgres + Redis running locally (see docs/OPERATIONS.md); then:
loadtests/run_stack.sh start
export DATABASE_URL=postgresql+psycopg2://qs:qs@127.0.0.1:5432/querysure_load REDIS_URL=redis://127.0.0.1:6379/0 \
       JWT_SECRET_KEY=load-test-secret-not-for-prod ENVIRONMENT=development ADMIN_EMAILS=admin@load.test
python loadtests/sprint4_load.py --users 10,25,50,100,200 --duration 45 --think 1.0 --label baseline
loadtests/run_stack.sh stop
```
Per user: create session, upload CSV (queued RQ job, polled to completion), then a weighted mix with ~1 s think time: list sessions / auth-me (40 %+15 %), generate-sql (25 %), execute-sql (15 %), re-upload (5 %).
Users are pre-seeded (verified, `business` plan so daily query caps do not end the run) and authenticated with directly minted JWTs, so per-IP register/login throttles do not dominate; auth endpoints are covered by the functional suite. Per-user rate limits (e.g. 30/min generate-sql) stay **on** and are counted as `429`.
Output: `loadtests/results/load_<label>.json` (client latency per operation, status codes, throughput, upload-to-ready job latency, server-side p50/p95/p99 and queue wait from `/api/admin/ops/summary`, 1 Hz saturation samples for Postgres connections, Redis ops/clients/memory, RQ depth, CPU/RSS).
Set `FAKE_LLM_FAIL_RATE=0.3` before starting the stack to exercise fallback / circuit breaker under load.

## Legacy Locust file
`locustfile.py` (Sprint 1) is unchanged.

## Sprint 4 closure tooling
* `worker_scaling.sh <workers> [users]`: same workload at N RQ workers (`WORKERS=N loadtests/run_stack.sh start`), with a 2 s `/api/ready` poller and queue/job accounting.
* `failure_injection.py` + `fake_llm_modes.py`: real SDK calls against a stub that injects timeout / 429 / 5xx / malformed / bad-SQL-then-garbage; checks fallback, breaker, budget and slot rollback, and canary leakage.
* `pool_exhaustion.py`: readiness/liveness while the DB pool is genuinely exhausted (held row lock).
* `alert_delivery.py`: production-config guards, real alert delivery to a local HTTPS receiver (provider + Redis outage, receiver down), and Sentry transport scrubbing.
* `burst_session.py`: simultaneous-burst check used to find the admission-control bug.
Results: `loadtests/results/`, summary: `docs/03_EVALUATION.md` (load and failure testing).
