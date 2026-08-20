#!/usr/bin/env bash
# Full-stack test: boots a real relay, enrolls a real agent, then drives a
# session through the same protocol path the browser viewer uses.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
PORT=${PORT:-7855}
TMP=$(mktemp -d)
export AEGISDESK_DIR="$TMP/agent"
export E2E_AGENT_PASSWORD="e2e-Test-Password-9"
mkdir -p "$AEGISDESK_DIR" "$TMP/relay"

PROBE="$TMP/probe.bin"
head -c 700000 /dev/urandom > "$PROBE"

cleanup() {
  [[ -n "${AGENT_PID:-}" ]] && kill "$AGENT_PID" 2>/dev/null
  [[ -n "${RELAY_PID:-}" ]] && kill "$RELAY_PID" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT

echo "=== booting relay on :$PORT (data in $TMP/relay) ==="
node relay/server.js --port "$PORT" --data-dir "$TMP/relay" --log info > "$TMP/relay.log" 2>&1 &
RELAY_PID=$!
for i in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null && break
  sleep 0.25
done
ADMIN_PW=$(grep -oP 'password : \K\S+' "$TMP/relay.log")
ENROLL=$(grep -A2 'Enrollment key' "$TMP/relay.log" | tail -1 | tr -d ' \r')
echo "relay up. admin password captured, enroll key ${ENROLL:0:8}..."

echo
echo "=== configuring + starting agent ==="
python3 - <<PY
import os, sys
sys.path.insert(0, "$ROOT/agent")
from aegis_agent.config import Config
c = Config()
c["relayUrl"] = "ws://127.0.0.1:$PORT"
c["enrollKey"] = "$ENROLL"
c["name"] = "E2E-POS-01"
c["group"] = "Test kitchen"
c["heartbeatSec"] = 5
c["logLevel"] = "info"
c.set_unattended_password(os.environ["E2E_AGENT_PASSWORD"])
c.save()
print("agent config written to", c.path)
PY

cd "$ROOT/agent"
python3 -m aegis_agent run --no-ui > "$TMP/agent.log" 2>&1 &
AGENT_PID=$!
cd "$ROOT"
sleep 3

echo
echo "=== driving the session as the viewer would ==="
node tests/e2e_viewer.mjs "http://127.0.0.1:$PORT" admin "$ADMIN_PW" "$PROBE"
RC=$?

echo
echo "=== agent log (tail) ==="
tail -22 "$TMP/agent.log"
echo
echo "=== relay log (tail) ==="
tail -14 "$TMP/relay.log"
echo
echo "=== agent session audit trail ==="
cat "$AEGISDESK_DIR/sessions.jsonl" 2>/dev/null | tail -8
echo
echo "=== relay audit trail ==="
tail -8 "$TMP/relay/audit.jsonl" 2>/dev/null

exit $RC
