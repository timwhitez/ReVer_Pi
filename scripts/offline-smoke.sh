#!/usr/bin/env sh
# Offline end-to-end smoke: local gateway + one Pi session + the ReVer-Pi extension.
#
# No credentials, no egress: the gateway runs with the `mock` provider (a protocol-only
# scripted model, never evidence of model quality) and binds to 127.0.0.1 only.
#
# Usage: sh scripts/offline-smoke.sh [output-dir] [port]
# Requires: python3 with this project's runtime dependencies, node >= 22.19,
#           and pi/node_modules installed (`cd pi && npm ci`).
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-"$ROOT/.smoke"}
PORT=${2:-8791}
cd "$ROOT"

if [ ! -f pi/node_modules/@earendil-works/pi-coding-agent/dist/cli.js ]; then
  echo "Pi dependencies missing: run 'cd pi && npm ci' first" >&2
  exit 2
fi
if [ -e "$OUT" ]; then
  echo "Output directory already exists (choose a new one): $OUT" >&2
  exit 2
fi

mkdir -p "$OUT/run" "$OUT/workspace"
printf 'Reply with exactly one JSON object: {"status":"ready"}\n' > "$OUT/prompt.md"
printf 'offline smoke fixture\n' > "$OUT/workspace/note.txt"

PYTHONPATH="$ROOT/src" python3 -m reverpi gateway \
  --provider configs/mock.chat.yaml --config configs/pilot.yaml --out "$OUT/run" \
  --host 127.0.0.1 --port "$PORT" > "$OUT/gateway.log" 2>&1 &
GW=$!
trap 'kill "$GW" 2>/dev/null || true' EXIT INT TERM

python3 - "$PORT" <<'PY'
import sys, time, urllib.request
url = f"http://127.0.0.1:{sys.argv[1]}/health"
for _ in range(80):
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            print("health:", response.read().decode().strip())
            raise SystemExit(0)
    except SystemExit:
        raise
    except Exception:
        time.sleep(0.5)
print("gateway did not become healthy; see the gateway log", file=sys.stderr)
raise SystemExit(1)
PY

PYTHONPATH="$ROOT/src" python3 -m reverpi session-create \
  --run "$OUT/run" --id smoke-1 --method mask --token-file "$OUT/token.txt"

PYTHONPATH="$ROOT/src" python3 -m reverpi pi-run \
  --cwd "$OUT/workspace" --prompt-file "$OUT/prompt.md" --out "$OUT/pi" \
  --gateway "http://127.0.0.1:$PORT" --token-file "$OUT/token.txt" \
  --wall-seconds 120 --max-tools 5 --max-turns 3 --acknowledge-unsandboxed

PYTHONPATH="$ROOT/src" python3 -m reverpi costs --run "$OUT/run"
echo
echo "offline smoke passed; artifacts under $OUT"
