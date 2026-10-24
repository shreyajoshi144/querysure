#!/usr/bin/env bash
# Start the full local stack for load testing: Postgres + Redis (must already run), moto S3, stub LLM, uvicorn (2 workers), RQ worker.
#   [WORKERS=N] loadtests/run_stack.sh start|stop     (N RQ workers, default 1)
set -uo pipefail
cd "$(dirname "$0")/.."
export DATABASE_URL="${LOAD_DATABASE_URL:-postgresql+psycopg2://qs:qs@127.0.0.1:5432/querysure_load}"
export REDIS_URL="redis://127.0.0.1:6379/0" ENVIRONMENT=development JWT_SECRET_KEY=load-test-secret-not-for-prod
export STORAGE_BACKEND=s3 S3_BUCKET=qs-load S3_ENDPOINT_URL=http://127.0.0.1:5555 S3_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
export GROQ_API_KEY=fake-load GROQ_BASE_URL=http://127.0.0.1:5556 GLOBAL_DAILY_AI_BUDGET_USD=100000
export ADMIN_EMAILS="${ADMIN_EMAILS:-admin@load.test}" LOG_LEVEL="${LOG_LEVEL:-INFO}" GAUGE_TTL_SECONDS=15
mkdir -p /tmp/qsload
case "${1:-start}" in
  start)
    "$0" stop >/dev/null 2>&1
    psql postgresql://qs:qs@127.0.0.1:5432/postgres -qc "DROP DATABASE IF EXISTS querysure_load" -c "CREATE DATABASE querysure_load OWNER qs"
    redis-cli flushall >/dev/null
    alembic upgrade head >/tmp/qsload/alembic.log 2>&1 || { echo "migration failed"; tail /tmp/qsload/alembic.log; exit 1; }
    (setsid nohup moto_server -H 127.0.0.1 -p 5555 >/tmp/qsload/moto.log 2>&1 &)
    (setsid nohup python loadtests/fake_llm.py 5556 400 >/tmp/qsload/llm.log 2>&1 &)
    sleep 2
    python -c "import boto3;boto3.client('s3',endpoint_url='http://127.0.0.1:5555').create_bucket(Bucket='qs-load')"
    (setsid nohup python -m uvicorn api:app --host 127.0.0.1 --port 8000 --workers 2 >/tmp/qsload/web.log 2>&1 &)
    for i in $(seq 1 "${WORKERS:-1}"); do (setsid nohup python worker.py >/tmp/qsload/worker$i.log 2>&1 &); done
    for i in $(seq 1 40); do curl -sf http://127.0.0.1:8000/api/ready >/dev/null && break; sleep 0.5; done
    curl -s http://127.0.0.1:8000/api/ready; echo ;;
  stop)
    pkill -f "uvicorn api:app" ; pkill -f "worker.py" ; pkill -f moto_server ; pkill -f fake_llm.py ; sleep 1 ;;
esac
