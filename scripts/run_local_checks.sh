#!/usr/bin/env sh
# Local verification. No CI, no network, no model calls.
#
# Usage: sh scripts/run_local_checks.sh [repo-root] [--with-smoke]
#        PYTHON=/path/to/python sh scripts/run_local_checks.sh
#
# Always runs: the CLI environment report, this repository's own test suite, and the
# fixture-free assurance contracts. Adds when available: the research regression
# suites (they need sealed fixtures that are not distributed), the Pi typecheck and
# Node tests (they need pi/node_modules), and with --with-smoke the end-to-end
# offline gateway/Pi smoke run.
set -u
ROOT=${1:-$(cd "$(dirname "$0")/.." && pwd)}
SMOKE=${2:-}
cd "$ROOT" || exit 2
PY=${PYTHON:-python3}
fail=0
step() { printf '\n$ %s\n' "$*"; "$@" || fail=1; }
skip() { printf '\n- skipped: %s\n' "$1"; }

command -v "$PY" >/dev/null 2>&1 || { echo "python not found: $PY" >&2; exit 2; }
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

step "$PY" -B -m reverpi doctor

"$PY" -c 'import pytest' 2>/dev/null || { echo "pytest missing for $PY (pip install pytest==9.0.2)" >&2; exit 2; }
"$PY" -c 'import pytest_asyncio' 2>/dev/null \
  || echo "note: pytest-asyncio missing; async runtime tests will error (pip install pytest-asyncio==1.3.0)"
"$PY" -c 'import scipy' 2>/dev/null \
  || echo "note: scipy missing; research/s3/tests/test_numeric.py needs it"

# This repository's own suite: passes in a clean checkout, skipping only tests whose
# sealed research inputs are not distributed.
step "$PY" -B -m pytest -q tests

# Fixture-free assurance contracts (also the subset that external reviewers can run).
step "$PY" -B -m pytest -q research/s5/tests/test_s5_contracts.py \
  research/s5/tests/test_s5_statistics.py research/s5/tests/test_s5_assurance.py

# The research regression suites need development source snapshots and raw-evidence
# fixtures; they run here when those are present in the checkout.
if [ -d research/s4/data ] && [ -d research/s4c/testdata ]; then
  step "$PY" -B -m pytest -q research/s3/tests research/s4/tests research/s5/tests
  step "$PY" -B -m pytest -q research/s4c/tests
else
  skip "research regression suites (snapshots/fixtures are not distributed)"
fi

if [ -d pi/tests ] && [ -d pi/node_modules ]; then
  step sh -c 'cd pi && ./node_modules/.bin/tsc --noEmit'
  step sh -c 'cd pi && node --experimental-strip-types --test tests/*.test.mjs'
  if [ -f research/s4/pi/readonly.test.mjs ]; then
    step node --experimental-strip-types --test research/s4/pi/readonly.test.mjs
  fi
else
  skip "Pi typecheck/tests (run: cd pi && npm ci)"
fi

if [ "$SMOKE" = "--with-smoke" ]; then
  tmp=$(mktemp -d)
  step env PYTHON="$PY" sh scripts/offline-smoke.sh "$tmp/smoke" 8799
else
  skip "offline end-to-end smoke (add --with-smoke, or run scripts/offline-smoke.sh)"
fi

if [ "$fail" -eq 0 ]; then printf '\n== local checks PASS ==\n'; else printf '\n== local checks FAIL ==\n'; fi
exit "$fail"
