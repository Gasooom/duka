#!/usr/bin/env bash
# Drive a conversation through the dev WhatsApp simulator (same pipeline as real webhooks).
# Usage: scripts/demo_chat.sh <owner-email> <customer-number> "msg 1" "msg 2" ...
set -euo pipefail
API=${API:-http://localhost:8000}
EMAIL=$1; FROM=$2; shift 2
# The password and the session token reach curl on stdin (printf is a shell builtin), never on its command line, where
# any local user could read them in the process list.
TOKEN=$(printf '{"email":"%s","password":"%s"}' "$EMAIL" "${PASSWORD:-password123}" \
  | curl -sf -XPOST "$API/api/auth/login" -H 'content-type: application/json' --data-binary @- \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
for m in "$@"; do
  echo "👤 $m"
  body=$(python3 -c "import json,sys;print(json.dumps({'text':sys.argv[1],'from_number':sys.argv[2],'name':'Demo Customer'}))" "$m" "$FROM")
  printf 'Authorization: Bearer %s\n' "$TOKEN" \
    | curl -sf -XPOST "$API/api/dev/simulate" -H @- -H 'content-type: application/json' -d "$body" \
    | python3 -c "import sys,json;d=json.load(sys.stdin);print('🤖 ['+d['status']+']', (d['reply'] or '').replace(chr(10), chr(10)+'   '));print()"
done
