#!/usr/bin/env bash
# Build and (re)start the production stack, then wait until it is ready through HTTPS.
#
#   deploy/deploy.sh                      # uses deploy/.env.production
#   DUKA_ENV_FILE=.env.staging deploy/deploy.sh
#
# Safe to re-run for upgrades: images are rebuilt, migrations run on backend start, data volumes are kept.
# Take a backup first for upgrades with migrations:  scripts/backup.sh (with COMPOSE set as below).
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=${DUKA_ENV_FILE:-.env.production}
[ -f "$ENV_FILE" ] || { echo "Create deploy/$ENV_FILE from deploy/.env.production.example first." >&2; exit 1; }
export DUKA_ENV_FILE="$ENV_FILE"
COMPOSE="docker compose --env-file $ENV_FILE -f docker-compose.prod.yml"
DOMAIN=$(grep -E '^DUKA_DOMAIN=' "$ENV_FILE" | cut -d= -f2)

$COMPOSE config --quiet
$COMPOSE build --pull
$COMPOSE up -d --remove-orphans

echo "waiting for https://$DOMAIN/readyz ..."
for _ in $(seq 1 90); do
  if body=$(curl -fsS ${CURL_INSECURE:+-k} "https://$DOMAIN/readyz" 2>/dev/null); then
    echo "ready: $body"
    $COMPOSE ps
    exit 0
  fi
  sleep 2
done
echo "not ready after 3 minutes; recent backend logs:" >&2
$COMPOSE logs --tail 50 backend >&2
exit 1
