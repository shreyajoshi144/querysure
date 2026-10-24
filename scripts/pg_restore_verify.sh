#!/usr/bin/env bash
# Restore a QuerySure dump into a THROW-AWAY database and verify it (Sprint 4). Never touches the source database.
#   DATABASE_URL=<source db url>  RESTORE_ADMIN_URL=postgresql://user:pass@host:5432/postgres  scripts/pg_restore_verify.sh <dump>
# Checks: sha256 matches; restore completes without errors; every public table exists with the SAME row count as the source;
# alembic_version equals the source's; the app's own migration check (`alembic check`) reports no drift on the restored copy.
# Use REQUIRE_SOURCE_MATCH=0 when verifying an OLD dump against a source that has moved on (then only integrity + schema are checked).
set -euo pipefail
DUMP="${1:?usage: pg_restore_verify.sh <dump file>}"
: "${DATABASE_URL:?}"; : "${RESTORE_ADMIN_URL:?}"
SRC="${DATABASE_URL/+psycopg2/}"
SCRATCH="qs_restore_verify_$(date -u +%s)"
ADMIN="$RESTORE_ADMIN_URL"
# scratch URL = admin URL with the database name replaced
SCRATCH_URL="${ADMIN%/*}/$SCRATCH"
START=$(date +%s)
if [ -f "$DUMP.sha256" ]; then ( cd "$(dirname "$DUMP")" && sha256sum -c "$(basename "$DUMP").sha256" ); fi
psql "$ADMIN" -v ON_ERROR_STOP=1 -qc "CREATE DATABASE $SCRATCH"
cleanup() { psql "$ADMIN" -qc "DROP DATABASE IF EXISTS $SCRATCH" >/dev/null 2>&1 || true; }
trap cleanup EXIT
pg_restore --no-owner --no-privileges --exit-on-error --dbname="$SCRATCH_URL" "$DUMP"
RESTORE_SECONDS=$(( $(date +%s) - START ))

count_sql="SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1"
FAIL=0
for t in $(psql "$SRC" -Atc "$count_sql"); do
  a=$(psql "$SRC" -Atc "SELECT count(*) FROM \"$t\"")
  b=$(psql "$SCRATCH_URL" -Atc "SELECT count(*) FROM \"$t\"" 2>/dev/null || echo MISSING)
  if [ "$a" != "$b" ]; then
    if [ "${REQUIRE_SOURCE_MATCH:-1}" = "1" ]; then echo "MISMATCH table=$t source=$a restored=$b"; FAIL=1; else echo "note table=$t source=$a restored=$b"; fi
  fi
done
V_SRC=$(psql "$SRC" -Atc "SELECT version_num FROM alembic_version")
V_RST=$(psql "$SCRATCH_URL" -Atc "SELECT version_num FROM alembic_version")
[ "$V_SRC" = "$V_RST" ] || { echo "alembic_version differs source=$V_SRC restored=$V_RST"; [ "${REQUIRE_SOURCE_MATCH:-1}" = "1" ] && FAIL=1; }
if command -v alembic >/dev/null 2>&1 && [ -f alembic.ini ]; then
  DATABASE_URL="${SCRATCH_URL/postgresql:/postgresql+psycopg2:}" alembic check >/dev/null 2>&1 && echo "alembic_check=clean" || { echo "alembic_check=DRIFT"; FAIL=1; }
fi
echo "restore_verify $( [ $FAIL = 0 ] && echo OK || echo FAILED ) alembic_version=$V_RST restore_seconds=$RESTORE_SECONDS"
exit $FAIL
