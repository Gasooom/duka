#!/usr/bin/env bash
# Drive a conversation through the dev WhatsApp simulator (same pipeline as real webhooks).
# Usage: scripts/demo_chat.sh <owner-email> <customer-number> "msg 1" "msg 2" ...
set -euo pipefail
API=${API:-http://localhost:8000}
EMAIL=$1; FROM=$2; shift 2
TOKEN=$(curl -sf -XPOST "$API/api/auth/login" -H 'content-type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"${PASSWORD:-password123}\"}" | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
for m in "$@"; do
  echo "👤 $m"
  body=$(python3 -c "import json,sys;print(json.dumps({'text':sys.argv[1],'from_number':sys.argv[2],'name':'Demo Customer'}))" "$m" "$FROM")
  curl -sf -XPOST "$API/api/dev/simulate" -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' -d "$body" \
    | python3 -c "import sys,json;d=json.load(sys.stdin);print('🤖 ['+d['status']+']', (d['reply'] or '').replace(chr(10), chr(10)+'   '));print()"
done
