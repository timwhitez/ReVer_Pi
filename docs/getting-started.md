# Getting started

## 1. Install

```bash
git clone https://github.com/timwhitez/ReVer_Pi.git
cd ReVer_Pi
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
reverpi doctor                      # environment report; calls nothing
cd pi && npm ci && npm run typecheck && cd ..
```

`reverpi doctor` reports Python, packages, executables, the code digest and whether Pi is installed.
It never opens a network connection. With `--provider configs/provider.chat.example.yaml` it also
reports whether the configured API-key environment variable is present, the mapped reasoning effort,
and whether currency pricing is configured — and still makes no request.

## 2. Verify offline, end to end, without credentials

```bash
sh scripts/offline-smoke.sh            # or: sh scripts/offline-smoke.sh /tmp/rever-smoke 8791
```

This starts the gateway on loopback with `configs/mock.chat.yaml` (a protocol-only mock model),
creates one session, runs Pi through the ReVer-Pi extension on a one-line prompt, and prints the
ledger totals. Expected output ends with `offline smoke passed` and a `costs` object whose
`accounted_usd` is `0.0` and `currency_is_fully_known` is `false` — the mock has no price, and the
ledger says so rather than guessing.

`configs/mock.chat.yaml` is a scripted transport, so **mock runs are never evidence about model
quality**. They exist to prove the wiring.

## 3. Configure a real provider

Copy an example and fill in your own values; never commit credentials.

```bash
cp configs/provider.chat.example.yaml configs/provider.local.yaml
$EDITOR configs/provider.local.yaml      # base_url, model, api_key_env, context_window, effort
export REVER_API_KEY='...'               # the variable named by api_key_env
```

- `base_url` must be the provider's own API root (for example `https://api.deepseek.com`), not a
  local proxy you do not control.
- `context_window` and `max_output_tokens` describe the *model*; the gateway uses them for admission.
- Reasoning dialects differ per provider: adjust `reasoning_style` and `effort_map`; see
  [configuration.md](configuration.md).

## 4. Run the agent

```bash
mkdir -p runs/demo
reverpi session-create --run runs/demo --id demo-1 --method mask \
  --token-file runs/demo/demo-1.token

# Terminal A: gateway (loopback only)
reverpi gateway --provider configs/provider.local.yaml --config configs/pilot.yaml \
  --out runs/demo --host 127.0.0.1 --port 8765 --allow-paid

# Terminal B: Pi through the extension
reverpi pi-run --cwd /path/to/project --prompt-file task.md --out runs/demo/pi \
  --gateway http://127.0.0.1:8765 --token-file runs/demo/demo-1.token \
  --acknowledge-unsandboxed --allow-paid
```

`--method` selects the context strategy for that session: `full` keeps everything, `mask` masks old
observations with exact archive handles, `archive` uses handle compaction, `rever_lite` is the
obligation-prioritised extractive selector. The full list is in [configuration.md](configuration.md).

Every session is single-use by design: the gateway refuses to replay a session that already has
attempts against it. Create a new session and a fresh run directory for each task.

## 5. Read the results

```bash
reverpi costs --run runs/demo                     # per-attempt accounting from the ledger
reverpi analyze --run runs/demo                   # paired statistics for a study run
reverpi reconcile --run runs/demo --attempt 3 --tokens 8123 --usd 0.0041 --evidence invoice.json
```

The run directory holds `sessions.sqlite`, `ledger.sqlite`, `archive.sqlite` and per-attempt wire
traces. When a provider bill disagrees with the ledger, `reconcile` records the correction against
the specific attempt and evidence file instead of editing history.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Pi dependencies missing` | Run `cd pi && npm ci`; the extension needs Pi's runtime, not just Node |
| `Pi changed the frozen reasoning effort` | Pi's thinking level differed from the session's; align `--thinking` with the provider config's `effort` |
| `Fresh task requires a new gateway session` | The session was reused after attempts were recorded; create a new session and output directory |
| `Real provider calls require --allow-paid` | Real dispatch is opt-in; pass `--allow-paid` deliberately |
| Ledger shows `unknown_attempts > 0` | An attempt was interrupted after dispatch; it is reported as unknown rather than assumed free — reconcile it with provider evidence |
| Research suites error with a missing module | Those suites need fixtures that ship only with the private research archive; use `sh scripts/run_local_checks.sh` for the repository's own contracts |
