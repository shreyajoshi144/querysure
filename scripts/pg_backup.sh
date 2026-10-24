#!/usr/bin/env bash
# QuerySure Postgres logical backup (Sprint 4).
#   DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/db  scripts/pg_backup.sh [output_dir]
# Produces  <dir>/querysure-<UTC timestamp>.dump  (pg_dump custom format, compressed) and a .sha256 next to it, then PROVES the
# archive is readable (pg_restore --list) before declaring success. Exit code != 0 means: do not trust this backup.
set -euo pipefail
: "${DATABASE_URL:?DATABASE_URL is required}"
OUT_DIR="${1:-./backups}"
URL="${DATABASE_URL/+psycopg2/}"          # SQLAlchemy driver suffix -> plain libpq URI
mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="$OUT_DIR/querysure-$STAMP.dump"
START=$(date +%s)
pg_dump --format=custom --compress=6 --no-owner --no-privileges --file="$FILE" "$URL"
( cd "$OUT_DIR" && sha256sum "$(basename "$FILE")" > "$(basename "$FILE").sha256" )
pg_restore --list "$FILE" > /dev/null          # structural integrity check of the archive
TABLES=$(pg_restore --list "$FILE" | grep -c " TABLE DATA " || true)
echo "backup_ok file=$FILE bytes=$(stat -c %s "$FILE") table_data_entries=$TABLES seconds=$(( $(date +%s) - START ))"
