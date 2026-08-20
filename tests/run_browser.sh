#!/usr/bin/env bash
# Boots a relay + agent, then drives the real viewer in a real Chromium.
# Needs playwright installed somewhere: npm install playwright
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
PORT=${PORT:-7877}
# playwright is resolved from ~/node_modules (outside the repo)
OUT=${OUT:-$ROOT/screenshots}
TMP=$(mktemp -d)
export AEGISDESK_DIR="$TMP/agent"
DEV_PASSWORD="Browser-Test-Pass-1"
mkdir -p "$AEGISDESK_DIR" "$TMP/relay" "$OUT"

cleanup() {
  [[ -n "${AGENT_PID:-}" ]] && kill "$AGENT_PID" 2>/dev/null
  [[ -n "${RELAY_PID:-}" ]] && kill "$RELAY_PID" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT

node relay/server.js --port "$PORT" --data-dir "$TMP/relay" > "$TMP/relay.log" 2>&1 &
RELAY_PID=$!
for i in $(seq 1 40); do curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null && break; sleep 0.25; done
ADMIN_PW=$(grep -oP 'password : \K\S+' "$TMP/relay.log")
ENROLL=$(grep -A2 'Enrollment key' "$TMP/relay.log" | tail -1 | tr -d ' \r')

python3 - <<PY
import sys; sys.path.insert(0, "$ROOT/agent")
from aegis_agent.config import Config
c = Config()
c["relayUrl"] = "ws://127.0.0.1:$PORT"
c["enrollKey"] = "$ENROLL"
c["name"] = "POS-01 (back office)"
c["group"] = "Front of house"
c["heartbeatSec"] = 5
c.set_unattended_password("$DEV_PASSWORD")
c.save()
PY

(cd "$ROOT/agent" && python3 -m aegis_agent run --no-ui > "$TMP/agent.log" 2>&1 &)
AGENT_PID=$!
sleep 4

CHROMIUM_PATH="${CHROMIUM_PATH:-$(ls -d /opt/pw-browsers/chromium-*/chrome-linux/chrome 2>/dev/null | head -1)}" \
  node tests/browser_check.mjs "http://127.0.0.1:$PORT" admin "$ADMIN_PW" "$DEV_PASSWORD" "$OUT"
RC=$?

echo
echo "=== agent log (tail) ==="
tail -12 "$TMP/agent.log"
exit $RC
