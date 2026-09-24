#!/bin/sh
# Local-only verification. No CI, no network, no model calls.
# Usage: sh scripts/run_local_checks.sh [repo-root]   (PYTHON=/path/to/python to pick an interpreter)
set -u
cd "${1:-$(dirname "$0")/..}" || exit 2
PY=${PYTHON:-python3}
fail=0
step() { printf '\n$ %s\n' "$*"; "$@" || fail=1; }
skip() { printf '\n- skipped: %s\n' "$1"; }

command -v "$PY" >/dev/null 2>&1 || { echo "python not found: $PY" >&2; exit 2; }
"$PY" -c 'import pytest' 2>/dev/null || { echo "pytest missing for $PY (pip install pytest==9.0.2)" >&2; exit 2; }
"$PY" -c 'import pytest_asyncio' 2>/dev/null \
  || echo "note: pytest-asyncio missing; async runtime tests will error (pip install pytest-asyncio==1.3.0)"
"$PY" -c 'import scipy' 2>/dev/null \
  || echo "note: scipy missing; research/s3/tests/test_numeric.py needs it"

# Fixture-free assurance contracts: runnable in every checkout of this branch family.
step "$PY" -B -m pytest -q research/s5/tests/test_s5_contracts.py \
  research/s5/tests/test_s5_statistics.py research/s5/tests/test_s5_assurance.py

# Everything below needs the development source snapshots and raw-evidence fixtures,
# which exist only in the full private S5 ZIP, not in this import.
if [ -d research/s4/data ] && [ -d research/s4c/testdata ]; then
  step "$PY" -B -m pytest -q tests research/s3/tests research/s4/tests research/s5/tests
  step "$PY" -B -m pytest -q research/s4c/tests
else
  skip "full regression suites (snapshots/fixtures live in the full private S5 ZIP)"
fi

if [ -d pi/tests ]; then
  if [ -d pi/node_modules ]; then
    step sh -c 'cd pi && ./node_modules/.bin/tsc --noEmit'
    step sh -c 'cd pi && node --experimental-strip-types --test tests/*.test.mjs'
  else
    skip "pi tests/typecheck (no pi/node_modules; run: cd pi && npm ci)"
  fi
  if [ -f research/s4/pi/readonly.test.mjs ] && [ -d pi/node_modules ]; then
    step node --experimental-strip-types --test research/s4/pi/readonly.test.mjs
  fi
fi

if [ "$fail" -eq 0 ]; then printf '\n== local checks PASS ==\n'; else printf '\n== local checks FAIL ==\n'; fi
exit "$fail"
