#!/usr/bin/env bash
# Quick-support flow: starts a relay, launches a support session the way the
# shipped .exe does, connects to it with the code, then checks that closing it
# leaves nothing behind on either machine.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
PORT=${PORT:-7920}
TMP=$(mktemp -d)
export AEGISDESK_DIR="$TMP/nothing-should-appear-here"
export PYTHONUNBUFFERED=1
mkdir -p "$TMP/relay"

cleanup() {
  [[ -n "${SPID:-}" ]] && kill "$SPID" 2>/dev/null
  [[ -n "${RPID:-}" ]] && kill "$RPID" 2>/dev/null
  return 0
}
trap cleanup EXIT

node relay/server.js --port "$PORT" --data-dir "$TMP/relay" > "$TMP/relay.log" 2>&1 &
RPID=$!
for i in $(seq 1 40); do curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null && break; sleep 0.25; done
ADMIN_PW=$(grep -oP 'password : \K\S+' "$TMP/relay.log")
ENROLL=$(grep -A2 'Enrollment key' "$TMP/relay.log" | tail -1 | tr -d ' \r')

echo "=== starting a support session (what double-clicking the .exe does) ==="
pushd agent >/dev/null
python3 -m aegis_agent support --relay "ws://127.0.0.1:$PORT" --enroll-key "$ENROLL" \
  --name "Maria on FRONT-DESK" > "$TMP/support.out" 2>&1 &
SPID=$!
popd >/dev/null

CODE=""; ID=""
for i in $(seq 1 50); do
  CODE=$(grep -oP 'Session code:\s+\K[0-9 ]+' "$TMP/support.out" | tail -1 | tr -d ' ')
  ID=$(grep -oP 'Your ID:\s+\K[0-9 ]+' "$TMP/support.out" | tail -1 | tr -d ' ')
  [[ -n "$CODE" && -n "$ID" ]] && break
  sleep 0.4
done
if [[ -z "$CODE" ]]; then echo "FAIL: no code was displayed"; cat "$TMP/support.out"; exit 1; fi
echo "    the person would read out:  ID $ID   code $CODE"
echo
sed -n '1,14p' "$TMP/support.out"
echo

node tests/support_check.mjs "http://127.0.0.1:$PORT" admin "$ADMIN_PW" "$CODE"
RC=$?

echo
echo "=== closing the window (what happens when they are done) ==="
kill "$SPID" 2>/dev/null
SPID=""
sleep 2.5

REMAINING=$(curl -sf "http://127.0.0.1:$PORT/healthz" | python3 -c "import sys,json; print(json.load(sys.stdin)['devices'])")
if [[ "$REMAINING" == "0" ]]; then
  echo "PASS  session de-enrolled itself, fleet list is clean again  (devices=$REMAINING)"
else
  echo "FAIL  a dead one-off session is still listed  (devices=$REMAINING)"
  RC=1
fi

echo
echo "=== did it leave anything on their computer? ==="
LEFTOVERS=$(find "$AEGISDESK_DIR" -type f 2>/dev/null | wc -l)
if [[ "$LEFTOVERS" == "0" ]]; then
  echo "PASS  nothing written to the agent config directory (no identity, no code, no logs)"
else
  echo "FAIL  left $LEFTOVERS file(s) behind:"
  find "$AEGISDESK_DIR" -type f 2>/dev/null
  RC=1
fi

if grep -qE '"kind": ?"support-session-(start|end)"' "$TMP/relay/audit.jsonl" 2>/dev/null; then
  echo "PASS  the relay still recorded the visit in its own audit log"
else
  echo "FAIL  no audit trail for the support session"
  RC=1
fi

exit $RC
