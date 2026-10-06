#!/usr/bin/env bash
# Prove a backup can be restored and used — not just that a file exists.
#
#   scripts/verify_restore.sh                         # newest backup in $BACKUP_DIR (default backups/)
#   scripts/verify_restore.sh backups/duka-....dump
#
# 1. restores into a scratch database (never touches the live one)
# 2. every table's row count must lie between the manifest's before/after counts taken around pg_dump
# 3. the real app code runs against the restored database: migrations at head, readiness checks, and a login
#    with a real user (password hash, tenant lookup) — then the scratch database is dropped.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=${COMPOSE:-docker compose}
# The same folder backup.sh writes to (BACKUP_DIR), so a scheduled run verifies the newest REAL backup.
DUMP=${1:-$(ls -1t "${BACKUP_DIR:-backups}"/duka-*.dump 2>/dev/null | head -1)}
[ -n "$DUMP" ] && [ -f "$DUMP" ] || { echo "no backup found" >&2; exit 1; }
DB_USER=${POSTGRES_USER:-$(grep -E '^POSTGRES_USER=' .env 2>/dev/null | cut -d= -f2 || true)}
DB_PASSWORD=${POSTGRES_PASSWORD:-$(grep -E '^POSTGRES_PASSWORD=' .env 2>/dev/null | cut -d= -f2 || true)}
DB_USER=${DB_USER:-commerce}; DB_PASSWORD=${DB_PASSWORD:-commerce}
SCRATCH="duka_verify_$(date -u +%Y%m%d%H%M%S)"
VERIFY_EMAIL=${VERIFY_EMAIL:-}       # optional: a real user to log in as on the restored copy
VERIFY_PASSWORD=${VERIFY_PASSWORD:-}
cleanup() { $COMPOSE exec -T db psql -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS \"$SCRATCH\"" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "verifying $DUMP"
scripts/restore.sh "$DUMP" "$SCRATCH"

MANIFEST="$DUMP.manifest"
fail=0
if [ -f "$MANIFEST" ]; then
  before=$(grep '^counts_before=' "$MANIFEST" | cut -d= -f2-)
  after=$(grep '^counts_after=' "$MANIFEST" | cut -d= -f2-)
  for pair in $before; do
    table=${pair%%=*}; lo=${pair#*=}
    hi=$(echo " $after " | grep -o " $table=[0-9]*" | cut -d= -f2)
    got=$($COMPOSE exec -T db psql -U "$DB_USER" -d "$SCRATCH" -At -c "SELECT count(*) FROM \"$table\"")
    if [ "$got" -lt "$lo" ] || [ "$got" -gt "${hi:-$lo}" ]; then
      echo "  MISMATCH $table: restored=$got expected $lo..${hi:-$lo}"; fail=1
    fi
  done
  [ $fail -eq 0 ] && echo "row counts ok ($(echo "$before" | wc -w) tables)"
else
  echo "warning: no manifest next to the dump; skipping row-count comparison"
fi

MSYS_NO_PATHCONV=1 $COMPOSE run --rm --no-deps -T \
  -e DATABASE_URL="postgresql+psycopg://$DB_USER:$DB_PASSWORD@db:5432/$SCRATCH" -e BACKGROUND_WORKERS=0 \
  -e VERIFY_EMAIL="$VERIFY_EMAIL" -e VERIFY_PASSWORD="$VERIFY_PASSWORD" backend python - <<'PY'
import os, sys
from sqlalchemy import select
from app.db.session import SessionLocal
from app.models import Business, User
from app import ops
from app.core.security import verify_password
with SessionLocal() as db:
    level, checks = ops.readiness(db, workers_running=False)
    critical = [c for c in checks if c.level == "down"]
    for c in checks:
        print(f"  {c.name}: {c.level} ({c.detail})")
    businesses = db.query(Business).count()
    user = db.scalar(select(User).where(User.email == os.environ["VERIFY_EMAIL"])) if os.environ.get("VERIFY_EMAIL") \
        else db.scalar(select(User).order_by(User.created_at))
    print(f"  businesses={businesses} sample_user={'yes' if user else 'none'}")
    ok_login = user is not None and db.get(Business, user.business_id) is not None and user.password_hash.startswith("$2")
    if ok_login and os.environ.get("VERIFY_PASSWORD"):
        ok_login = verify_password(os.environ["VERIFY_PASSWORD"], user.password_hash)
        print(f"  login as {user.email}: {'ok' if ok_login else 'FAILED'}")
    sys.exit(0 if not critical and ok_login else 1)
PY
app_status=$?
[ $fail -eq 0 ] && [ $app_status -eq 0 ] && echo "RESTORE VERIFIED: $DUMP" || { echo "RESTORE VERIFICATION FAILED: $DUMP" >&2; exit 1; }
