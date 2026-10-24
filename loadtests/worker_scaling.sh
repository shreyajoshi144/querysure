#!/usr/bin/env bash
# Sprint 4 closure: same 100-user workload at 1/2/4 RQ workers, each on a fresh DB + Redis. Usage: loadtests/worker_scaling.sh <workers> [users=100]
set -uo pipefail
cd "$(dirname "$0")/.."
W="${1:?workers}"; U="${2:-100}"; L="w${W}_u${U}"
WORKERS=$W loadtests/run_stack.sh start >/tmp/qsload/stack_$L.log 2>&1 || { echo "stack failed"; cat /tmp/qsload/stack_$L.log; exit 1; }
export DATABASE_URL="postgresql+psycopg2://qs:qs@127.0.0.1:5432/querysure_load" REDIS_URL=redis://127.0.0.1:6379/0 JWT_SECRET_KEY=load-test-secret-not-for-prod ENVIRONMENT=development ADMIN_EMAILS=admin@load.test
: > /tmp/qsload/ready_$L.log
( while true; do s=$(date +%s.%N); c=$(curl -s -o /dev/null -w "%{http_code}" -m 15 http://127.0.0.1:8000/api/ready); e=$(date +%s.%N); echo "$c $(echo "($e-$s)*1000/1" | bc)" >> /tmp/qsload/ready_$L.log; sleep 2; done ) &
POLL=$!
python loadtests/sprint4_load.py --users $U --duration 60 --think 1.0 --label "workers_$L" --out loadtests/results > /tmp/qsload/scale_$L.log 2>&1
END=$(date +%s)
OPEN=$(psql postgresql://qs:qs@127.0.0.1:5432/querysure_load -Atc "select count(*) from jobs where kind='dataset_ingestion' and status in ('queued','running','retrying')")
DEPTH=$(redis-cli llen rq:queue:querysure)
DONE=$(psql postgresql://qs:qs@127.0.0.1:5432/querysure_load -Atc "select count(*) from jobs where kind='dataset_ingestion' and status='succeeded'")
FAILED=$(psql postgresql://qs:qs@127.0.0.1:5432/querysure_load -Atc "select count(*) from jobs where kind='dataset_ingestion' and status='failed'")
# time to drain whatever is left
D0=$(date +%s); for i in $(seq 1 90); do n=$(redis-cli llen rq:queue:querysure); [ "$n" = "0" ] && break; sleep 2; done; DRAIN=$(( $(date +%s) - D0 ))
kill $POLL 2>/dev/null
echo "{\"workers\": $W, \"users\": $U, \"open_ingestion_jobs_at_phase_end\": $OPEN, \"queue_depth_at_phase_end\": $DEPTH, \"ingestion_succeeded\": $DONE, \"ingestion_failed\": $FAILED, \"drain_seconds_after_phase\": $DRAIN}" > /tmp/qsload/scale_extra_$L.json
cat /tmp/qsload/scale_extra_$L.json
loadtests/run_stack.sh stop >/dev/null 2>&1
