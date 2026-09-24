# Agent installation and verification protocol

Canonical procedure for coding agents (Codex, Claude Code, …) installing, configuring or verifying
ReVer-Pi from a source checkout. Follow the phases in order; explicit user instructions win.

Installation is complete only when Pi stays unmodified, the local checks pass, the offline smoke run
finishes with a healthy gateway and an accounted attempt, and no credential is written to a file.

## Rules

- Do not patch, fork or vendor Pi. The extension must load through Pi's public package interface.
- Use Python 3.11+ and Node 22.19+, with Pi `@earendil-works/pi-coding-agent@0.84.2`. A different Pi
  version is a compatibility change: re-run the full suite before use.
- Never print, log, commit or upload a secret. Check *whether* a credential exists, never its value.
- Keep provider settings in a git-ignored `configs/provider.local.yaml`; keep study settings in
  `configs/`. Do not commit run directories.
- Do not clean, reset or overwrite unrelated working-tree changes.
- There is no CI in this repository. Local checks are the verification record; report the commands
  and exit codes you actually ran.

## Phase 1 — validate the checkout

```bash
git status --short --branch
git rev-parse HEAD
python3 --version && node --version
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
cd pi && npm ci && npm run typecheck && npm test && cd ..
```

Require exit 0 from the typecheck and the Pi test suite (116 tests). `npm ci` is the only network step.

## Phase 2 — verify offline behaviour

```bash
sh scripts/run_local_checks.sh      # exit 0; prints explicit skips for fixture-dependent suites
sh scripts/offline-smoke.sh /tmp/rever-smoke
```

The smoke run must print `offline smoke passed`, one accounted attempt, `accounted_usd` `0.0` and
`currency_is_fully_known` `false`. If the gateway does not become healthy, read
`/tmp/rever-smoke/gateway.log` — do not start a real provider to "fix" a mock failure.

## Phase 3 — configure a real provider (only when the user asks)

1. Copy an example: `cp configs/provider.chat.example.yaml configs/provider.local.yaml`.
2. Fill in `base_url`, `model`, `context_window`, `max_output_tokens`, `effort_map`, `reasoning_style`.
3. Confirm the credential is present without printing it: `test -n "$REVER_API_KEY"`.
4. Run `reverpi doctor --provider configs/provider.local.yaml` and check
   `secret_present: true`, `currency_pricing_configured`, and that `mock` is `false`.
5. A first real call is a paid action: use `--allow-paid` only with the user's explicit approval and a
   frozen budget in the study config.

## Phase 4 — run a task

```bash
mkdir -p runs/task-1
reverpi session-create --run runs/task-1 --id task-1 --method mask --token-file runs/task-1/task-1.token
reverpi gateway --provider configs/provider.local.yaml --config configs/pilot.yaml --out runs/task-1 \
  --host 127.0.0.1 --port 8765 [--allow-paid]
reverpi pi-run --cwd <project> --prompt-file <task.md> --out runs/task-1/pi \
  --gateway http://127.0.0.1:8765 --token-file runs/task-1/task-1.token \
  --acknowledge-unsandboxed [--allow-paid]
reverpi costs --run runs/task-1
```

One session per task, one task per run directory. Never reuse a session that already has attempts.

## Phase 5 — report

Report: the commit SHA, the commands with exit codes, the methods and budget used, the ledger totals
including `unknown_attempts`, and anything that could not be verified. Do not describe mock results
as model-quality evidence, and do not publish cost-savings claims from a run whose currency status is
unknown.
