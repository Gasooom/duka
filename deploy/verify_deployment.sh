#!/usr/bin/env bash
# Black-box checks of a running production deployment (run on/against the server after deploy.sh).
#
#   DUKA_ENV_FILE=.env.production deploy/verify_deployment.sh
#   (local rehearsal with Caddy's internal CA: CURL_INSECURE=1 DUKA_ENV_FILE=.env.rehearsal deploy/verify_deployment.sh)
#
# Creates nothing customer-visible except, with CREATE_TEST_TENANT=1, a throwaway tenant used to log in.
set -uo pipefail
cd "$(dirname "$0")"
ENV_FILE=${DUKA_ENV_FILE:-.env.production}
export DUKA_ENV_FILE="$ENV_FILE"
COMPOSE="docker compose --env-file $ENV_FILE -f docker-compose.prod.yml"
val() { grep -E "^$1=" "$ENV_FILE" | cut -d= -f2-; }
DOMAIN=$(val DUKA_DOMAIN); OPS=$(val OPS_TOKEN); VERIFY=$(val WHATSAPP_VERIFY_TOKEN); APPSECRET=$(val WHATSAPP_APP_SECRET)
K=${CURL_INSECURE:+-k}
BASE="https://$DOMAIN"
pass=0; fail=0
check() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1"; fail=$((fail+1)); fi; }
code() { curl -s $K -o /dev/null -w '%{http_code}' "$@"; }

check "HTTP redirects to HTTPS"                 '[ "$(curl -s -o /dev/null -w "%{http_code}" http://$DOMAIN/login)" = 308 ]'
check "liveness over HTTPS"                     '[ "$(code $BASE/healthz)" = 200 ]'
check "readiness over HTTPS"                    'curl -fs $K $BASE/readyz | grep -q "\"ok\""'
check "dashboard served"                        '[ "$(code $BASE/login)" = 200 ]'
check "HSTS header"                             'curl -sI $K $BASE/login | grep -qi "strict-transport-security"'
check "nosniff + frame deny headers"            'curl -sI $K $BASE/login | grep -qi "x-content-type-options: nosniff" && curl -sI $K $BASE/login | grep -qi "x-frame-options: deny"'
check "no Server header"                        '! curl -sI $K $BASE/login | grep -qi "^server:"'
check "API docs disabled"                       '[ "$(code $BASE/api/../docs)" != 200 ] && [ "$(code $BASE/openapi.json)" != 200 ]'
check "metrics hidden without token"            '[ "$(code $BASE/metrics)" = 404 ]'
check "metrics with ops token"                  'curl -fs $K -H "Authorization: Bearer $OPS" $BASE/metrics | grep -q "^duka_up 1"'
check "readiness details only with token"       '! curl -s $K "$BASE/readyz?details=1" | grep -q checks && curl -s $K -H "Authorization: Bearer $OPS" "$BASE/readyz?details=1" | grep -q checks'
check "public registration closed"              '[ "$(code -X POST -H content-type:application/json -d "{\"business_name\":\"X Y\",\"email\":\"x@y.rw\",\"password\":\"password123\"}" $BASE/api/auth/register)" = 403 ]'
check "Meta verify handshake echoes challenge"  '[ "$(curl -s $K "$BASE/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=$VERIFY&hub.challenge=4242")" = 4242 ]'
check "Meta verify rejects wrong token"         '[ "$(code "$BASE/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=1")" = 403 ]'
check "unsigned webhook rejected"               '[ "$(code -X POST -H content-type:application/json -d "{}" $BASE/webhooks/whatsapp)" = 401 ]'
BODY='{"object":"whatsapp_business_account","entry":[]}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$APPSECRET" | sed 's/^.* //')
check "signed webhook accepted"                 '[ "$(code -X POST -H content-type:application/json -H "X-Hub-Signature-256: sha256=$SIG" -d "$BODY" $BASE/webhooks/whatsapp)" = 200 ]'
unpublished() { [ "$(docker inspect -f "{{json .HostConfig.PortBindings}}" "$($COMPOSE ps -q "$1")")" = "{}" ]; }
check "PostgreSQL not published"                'unpublished db'
check "API not published (only via proxy)"      'unpublished backend'
check "dashboard not published (only via proxy)" 'unpublished frontend'
check "restart policies set"                    '[ "$(docker inspect -f "{{.HostConfig.RestartPolicy.Name}}" $($COMPOSE ps -q) | sort -u)" = unless-stopped ]'
check "log rotation configured"                 '[ "$(docker inspect -f "{{index .HostConfig.LogConfig.Config \"max-size\"}}" $($COMPOSE ps -q backend))" = 20m ]'
check "no test tooling in the API image"        '! $COMPOSE exec -T backend python -c "import pytest"'
check "no tests, evals or demo seed in the image" '$COMPOSE exec -T backend sh -c "test ! -e tests && test ! -e evals && test ! -e seed"'
check "API runs as non-root"                    '[ "$($COMPOSE exec -T backend id -u)" != 0 ]'
check "secrets not in logs"                     '! $COMPOSE logs --no-color 2>/dev/null | grep -qF "$VERIFY"'
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
