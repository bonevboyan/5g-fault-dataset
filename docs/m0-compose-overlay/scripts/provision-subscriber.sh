#!/usr/bin/env bash
# Provision the default test subscriber into free5GC via the webconsole API.
set -euo pipefail
cd "$(dirname "$0")/.."
UEID="${1:-imsi-208930000000001}"
PLMN="${2:-20893}"
BASE="http://localhost:5000"

TOKEN=$(curl -s -X POST "$BASE/api/login" -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"free5gc"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
[ -n "$TOKEN" ] || { echo "login failed"; exit 1; }

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/subscriber/$UEID/$PLMN" \
  -H "Token: $TOKEN" -H 'Content-Type: application/json' --data @subscriber.json)
echo "provision $UEID: HTTP $code"
[ "$code" = "201" ] || [ "$code" = "200" ] || { echo "provisioning failed"; exit 1; }
