#!/usr/bin/env bash
# Everything: unit tests, Python<->WebCrypto interop, then a live full-stack session.
set -uo pipefail
cd "$(dirname "$0")/.."

echo "############ 1/5  agent unit tests ############"
python3 -m unittest discover -s tests -p 'test_agent.py' -v 2>&1 | tail -5
A=$?

echo
echo "############ 2/5  python <-> browser crypto interop ############"
python3 -m unittest discover -s tests -p 'test_crypto_interop.py' -v 2>&1 | tail -5
B=$?

echo
echo "############ 3/5  live relay + agent + viewer session ############"
bash tests/run_e2e.sh 2>&1 | grep -E '^(PASS|FAIL|===|  [0-9]+/)'
C=$?

echo
echo "############ 4/5  quick-support flow ############"
bash tests/run_support.sh 2>&1 | grep -E '^(PASS|FAIL|  [0-9]+/)'
S=$?

D=0
if [[ -d "$HOME/node_modules/playwright" || -d node_modules/playwright || -d ../node_modules/playwright ]]; then
  echo
  echo "############ 5/5  real browser (Chromium) ############"
  bash tests/run_browser.sh 2>&1 | grep -E '^(PASS|FAIL|  [0-9]+/|  screenshots)'
  D=$?
else
  echo
  echo "(skipping the browser suite -- run 'npm install playwright' to enable it)"
fi

echo
if [[ $A -eq 0 && $B -eq 0 && $C -eq 0 && $S -eq 0 && $D -eq 0 ]]; then
  echo "ALL GREEN"
else
  echo "FAILURES: unit=$A interop=$B e2e=$C support=$S browser=$D"
fi
exit $(( A | B | C | S | D ))
