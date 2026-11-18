#!/usr/bin/env bash
# Production-mode rehearsal on one machine (Sprint 13): builds the real image and starts the real compose services (PostgreSQL, Redis,
# api, worker, scheduler) with ENVIRONMENT=production -- so the production guards run, the container applies `alembic upgrade head`
# on boot and datasets/reports go to an S3-compatible store (MinIO) -- in an ISOLATED project with throw-away credentials and its own volumes, runs the smoke test, takes a backup,
# restores it into a scratch database and verifies it, restarts the services to prove data and migrations survive, then removes
# everything it created. It never touches other compose projects or their volumes.   Needs Docker.   Usage: scripts/launch_rehearsal.sh
set -uo pipefail
cd "$(dirname "$0")/.."
P=qsrehearsal; PORT=${REHEARSAL_PORT:-5099}
export POSTGRES_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_hex(16))')" JWT_SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
export POSTGRES_USER=querysure POSTGRES_DB=querysure GROQ_API_KEY= OPENAI_API_KEY= ANTHROPIC_API_KEY=
FERNET="$(python3 -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"; S3K="$(python3 -c 'import secrets;print(secrets.token_hex(12))')"
OVR="$(mktemp)"; cat > "$OVR" <<YML
x-prod: &prod
  ENVIRONMENT: production
  S3_BUCKET: qsbucket
  S3_ENDPOINT_URL: http://minio:9000
  S3_REGION: us-east-1
  AWS_ACCESS_KEY_ID: qsrehearsal
  AWS_SECRET_ACCESS_KEY: $S3K
  CORS_ALLOWED_ORIGINS: https://qs.example.test
  TRUSTED_PROXY_IPS: 127.0.0.1
  APP_BASE_URL: https://qs.example.test
  EMAIL_PROVIDER: smtp
  SMTP_HOST: mail.invalid
  SMTP_USERNAME: rehearsal
  SMTP_PASSWORD: rehearsal
  EMAIL_FROM: QuerySure <noreply@qs.example.test>
  CONNECTIONS_ENCRYPTION_KEY: $FERNET
services:
  minio:
    image: cgr.dev/chainguard/minio:latest
    command: ["server", "/data"]
    environment: {MINIO_ROOT_USER: qsrehearsal, MINIO_ROOT_PASSWORD: $S3K}
  minio-init:
    image: cgr.dev/chainguard/minio-client:latest-dev
    entrypoint: ["sh", "-c", "for i in \$\$(seq 1 40); do mc alias set l http://minio:9000 qsrehearsal $S3K >/dev/null 2>&1 && mc mb -p l/qsbucket && exit 0; sleep 1; done; exit 1"]
    depends_on: [minio]
    restart: "no"
  db: {ports: !reset []}
  redis: {ports: !reset []}
  api: {ports: !override ["127.0.0.1:$PORT:5000"], environment: *prod, depends_on: {minio-init: {condition: service_completed_successfully}}}
  worker: {environment: *prod, depends_on: {minio-init: {condition: service_completed_successfully}}}
  scheduler: {environment: *prod}
YML
DC=(docker compose -p $P -f docker-compose.yml -f "$OVR")
FAIL=0; t0=$(date +%s)
ok() { echo "PASS  $1"; }; bad() { echo "FAIL  $1"; FAIL=1; }
cleanup() { "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1; rm -f "$OVR"; rm -rf "$BK"; }
BK="$(mktemp -d)"; trap cleanup EXIT

"${DC[@]}" build >/dev/null 2>&1 && ok "image builds ($(( $(date +%s) - t0 ))s)" || { bad "image build"; exit 1; }
UPOUT=$("${DC[@]}" up -d 2>&1) || { bad "stack starts"; echo "$UPOUT" | tail -8 | sed "s/^/      /"; exit 1; }
for i in $(seq 1 60); do
  H=$("${DC[@]}" ps --format '{{.Service}}={{.Health}}' | tr '\n' ' ')
  [[ "$H" == *api=healthy* && "$H" == *worker=healthy* ]] && break; sleep 3
done
echo "      services: $H"
[[ "$H" == *api=healthy* && "$H" == *worker=healthy* ]] && ok "api and worker healthy" || { bad "api/worker healthy"; "${DC[@]}" logs --tail 30 api | sed 's/^/      /'; exit 1; }
VERIFY="${DC[*]} exec -T db psql -U querysure -qc \"update users set email_verified=true where email='{email}'\""
python3 scripts/launch_smoke.py "http://127.0.0.1:$PORT" --verify-cmd "$VERIFY" && ok "smoke test (throw-away account created and deleted)" || bad "smoke test"
OUT=$(python3 scripts/launch_smoke.py "http://127.0.0.1:$PORT" --verify-cmd "$VERIFY" --keep-account 2>&1) && ok "second account left in place as backup payload" || { bad "payload account"; echo "$OUT" | grep -v '^PASS' | sed 's/^/      /'; }
NET="${P}_default"; DBURL="postgresql+psycopg2://querysure:$POSTGRES_PASSWORD@db:5432/querysure"
PG=(docker run --rm --network "$NET" -v "$PWD/scripts:/s:ro" -v "$BK:/b" -e DATABASE_URL="$DBURL" -e RESTORE_ADMIN_URL="postgresql://querysure:$POSTGRES_PASSWORD@db:5432/postgres" postgres:16)
OUT=$("${PG[@]}" bash /s/pg_backup.sh /b 2>&1) && ok "backup: $(echo "$OUT" | tail -1 | sed 's#file=[^ ]*##')" || { bad "backup"; echo "$OUT" | tail -5 | sed 's/^/      /'; }
DUMP=$(ls "$BK"/*.dump 2>/dev/null | head -1)
OUT=$("${PG[@]}" bash /s/pg_restore_verify.sh "/b/$(basename "$DUMP")" 2>&1) && ok "restore into scratch DB verified: $(echo "$OUT" | tail -1)" || { bad "restore verify"; echo "$OUT" | tail -8 | sed 's/^/      /'; }
"${DC[@]}" exec -T api alembic current 2>&1 | tail -1 | sed 's/^/      alembic current: /'
"${DC[@]}" exec -T api alembic check 2>&1 | tail -1 | sed 's/^/      alembic check: /'
"${DC[@]}" restart api worker scheduler >/dev/null 2>&1
for i in $(seq 1 40); do curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break; sleep 2; done
curl -fs "http://127.0.0.1:$PORT/api/ready" >/dev/null && ok "restart: api ready again" || bad "restart ready"
N=$("${DC[@]}" exec -T db psql -U querysure -Atc "select count(*) from users" | tr -d '\r')
[ "${N:-0}" -ge 1 ] && ok "restart: data survived ($N user row(s))" || bad "data survived"
"${DC[@]}" exec -T scheduler python -c "import os,time,sys; sys.exit(0 if time.time()-os.path.getmtime('/tmp/qs-scheduler.alive')<180 else 1)" && ok "scheduler heartbeat fresh" || bad "scheduler heartbeat"
echo "rehearsal $([ $FAIL = 0 ] && echo PASSED || echo FAILED) in $(( $(date +%s) - t0 ))s"; exit $FAIL
