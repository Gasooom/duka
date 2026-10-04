#!/usr/bin/env bash
# Restore a Duka backup.
#
#   scripts/restore.sh backups/duka-20261004T020000Z.dump               # into a NEW database duka_restore_<ts>
#   scripts/restore.sh backups/duka-....dump my_restore_db              # into a new database with that name
#   scripts/restore.sh backups/duka-....dump --replace-live             # disaster recovery: replace the live DB
#
# --replace-live stops the backend/frontend, renames the live database to <name>_before_restore_<ts> (kept, not
# dropped), restores into a fresh database with the live name, then starts the services again.
set -euo pipefail
cd "$(dirname "$0")/.."

DUMP=${1:?usage: scripts/restore.sh <file.dump> [target_db | --replace-live]}
TARGET=${2:-}
COMPOSE=${COMPOSE:-docker compose}
DB_USER=${POSTGRES_USER:-$(grep -E '^POSTGRES_USER=' .env 2>/dev/null | cut -d= -f2 || true)}
DB_NAME=${POSTGRES_DB:-$(grep -E '^POSTGRES_DB=' .env 2>/dev/null | cut -d= -f2 || true)}
DB_USER=${DB_USER:-commerce}; DB_NAME=${DB_NAME:-commerce}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

[ -f "$DUMP" ] || { echo "no such file: $DUMP" >&2; exit 1; }
if [ -f "$DUMP.sha256" ]; then
  (cd "$(dirname "$DUMP")" && sha256sum -c "$(basename "$DUMP").sha256" >/dev/null) || { echo "checksum mismatch: $DUMP" >&2; exit 1; }
  echo "checksum ok"
fi

psql_admin() { $COMPOSE exec -T db psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 -At -c "$1"; }
restore_into() {
  psql_admin "CREATE DATABASE \"$1\""
  $COMPOSE exec -T db psql -U "$DB_USER" -d "$1" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null
  $COMPOSE exec -T db pg_restore -U "$DB_USER" -d "$1" --no-owner --no-privileges --exit-on-error < "$DUMP"
}

if [ "$TARGET" = "--replace-live" ]; then
  read -r -p "Replace live database '$DB_NAME' with $DUMP? The current one is kept as ${DB_NAME}_before_restore_$STAMP. Type RESTORE: " ok
  [ "$ok" = "RESTORE" ] || { echo "aborted"; exit 1; }
  $COMPOSE stop backend frontend
  psql_admin "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DB_NAME' AND pid <> pg_backend_pid()" >/dev/null
  psql_admin "ALTER DATABASE \"$DB_NAME\" RENAME TO \"${DB_NAME}_before_restore_$STAMP\""
  restore_into "$DB_NAME"
  $COMPOSE start backend frontend
  echo "live database replaced from $DUMP (previous copy: ${DB_NAME}_before_restore_$STAMP)"
else
  TARGET=${TARGET:-duka_restore_$STAMP}
  [ "$TARGET" != "$DB_NAME" ] || { echo "refusing to restore over the live database; use --replace-live" >&2; exit 1; }
  restore_into "$TARGET"
  echo "restored into database '$TARGET'"
fi
