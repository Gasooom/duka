#!/usr/bin/env bash
# Back up the Duka PostgreSQL database (consistent snapshot, custom format) with a manifest for verification.
#
#   scripts/backup.sh                      # writes backups/duka-<UTC timestamp>.dump (+ .manifest, .sha256)
#   BACKUP_DIR=/srv/duka/backups BACKUP_RETENTION_DAYS=14 scripts/backup.sh
#
# Schedule it (host crontab):  15 2 * * *  cd /opt/duka && scripts/backup.sh >> /var/log/duka-backup.log 2>&1
# A backup on the same server does not survive the loss of that server: copy BACKUP_DIR off the machine
# (see docs/OPERATIONS.md). Verify regularly with scripts/verify_restore.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077  # dumps hold customer phone numbers and conversations: readable by the backup owner only

COMPOSE=${COMPOSE:-docker compose}
BACKUP_DIR=${BACKUP_DIR:-backups}
RETENTION_DAYS=${BACKUP_RETENTION_DAYS:-14}
DB_USER=${POSTGRES_USER:-$(grep -E '^POSTGRES_USER=' .env 2>/dev/null | cut -d= -f2 || echo commerce)}
DB_NAME=${POSTGRES_DB:-$(grep -E '^POSTGRES_DB=' .env 2>/dev/null | cut -d= -f2 || echo commerce)}
DB_USER=${DB_USER:-commerce}; DB_NAME=${DB_NAME:-commerce}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$BACKUP_DIR/duka-$STAMP.dump"
mkdir -p "$BACKUP_DIR"

psql_q() { $COMPOSE exec -T db psql -U "$DB_USER" -d "$DB_NAME" -At -c "$1"; }
counts() {
  psql_q "SELECT string_agg(format('%s=%s', t, (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I', t), false, true, '')))[1]::text), ' ' ORDER BY t)
          FROM unnest(ARRAY(SELECT tablename FROM pg_tables WHERE schemaname = 'public')) AS t"
}

BEFORE=$(counts)
$COMPOSE exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" --format=custom --no-owner --no-privileges > "$OUT.part"
AFTER=$(counts)
mv "$OUT.part" "$OUT"
REVISION=$(psql_q "SELECT version_num FROM alembic_version")
{
  echo "created_utc=$STAMP"
  echo "database=$DB_NAME"
  echo "alembic_revision=$REVISION"
  echo "counts_before=$BEFORE"
  echo "counts_after=$AFTER"
} > "$OUT.manifest"
(cd "$BACKUP_DIR" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")
SIZE=$(du -h "$OUT" | cut -f1)
echo "backup ok: $OUT ($SIZE, revision $REVISION)"

# Retention: remove backups older than RETENTION_DAYS (and their manifest/checksum).
find "$BACKUP_DIR" -name 'duka-*.dump*' -type f -mtime "+$RETENTION_DAYS" -print -delete | sed 's/^/pruned: /'
