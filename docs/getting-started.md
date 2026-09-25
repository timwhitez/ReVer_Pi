# Getting started

## 1. Install

Installation needs network access (git, PyPI, npm). Everything after it — `doctor`, the offline
smoke and the local checks — makes no network request.

```bash
git clone https://github.com/timwhitez/ReVer_Pi.git
cd ReVer_Pi
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'            # '.[test]' adds pytest/pytest-asyncio for the local checks
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

The product path is: start the gateway (the only place paid dispatch is authorised) → create a
session → run Pi through the extension → read the ledger. Each command below is checked against the
current CLI, and the whole block is executed end to end with the mock Provider by
`tests/test_docs_commands.py`.

<!-- doc-contract: product-path -->
```bash
# Terminal A: gateway on loopback. --allow-paid is the explicit authorisation for real
# Provider calls; it is not needed (and changes nothing) for the mock Provider.
reverpi gateway --provider configs/provider.local.yaml --config configs/pilot.yaml \
  --out runs/demo --host 127.0.0.1 --port 8765 --allow-paid

# Terminal B: one single-use session, then Pi through the extension.
reverpi session-create --run runs/demo --id demo-1 --method mask \
  --token-file runs/demo/demo-1.token
reverpi pi-run --cwd /path/to/project --prompt-file task.md --out runs/demo/pi \
  --gateway http://127.0.0.1:8765 --token-file runs/demo/demo-1.token \
  --acknowledge-unsandboxed
reverpi costs --run runs/demo
```

`pi-run` holds only the session token; it cannot authorise spending. `--acknowledge-unsandboxed`
confirms that Pi's own tools run directly in `--cwd`, outside any sandbox.

Every session is single-use by design: the gateway refuses to replay a session that already has
attempts against it. Create a new session (and, for a new configuration, a fresh run directory) for
each task.

### Context strategy vs. online projection

These are two different mechanisms:

- **`--method`** (per session) selects how Pi's *full-history compaction* is performed when Pi
  decides to compact: `mask` masks old observations to excerpts with exact archive handles,
  `archive` uses handle compaction, `rever_lite` is the obligation-prioritised extractive selector,
  `pi_native`/`pi_original` keep Pi's own compactor. The method must be listed in the study
  config's `methods`. The full list is in [configuration.md](configuration.md).
- **`online_projection.mode`** (per gateway, in the study config; default `off`) projects eligible
  old tool observations *in each outgoing request*, before Pi compacts anything. Setting
  `--method mask` alone does **not** enable it.

To try online projection with the split `search_evidence` / `recover_evidence` tools, start the
gateway with the example study config and a `mask` session (a `pi_original` session on the same
gateway is the unprojected baseline):

<!-- doc-contract: online-path -->
```bash
reverpi gateway --provider configs/mock.chat.yaml --config configs/online-projection.example.yaml \
  --out runs/online --host 127.0.0.1 --port 8765
reverpi session-create --run runs/online --id online-1 --method mask --token-file runs/online/online-1.token
```

A live Provider is admitted for online projection only under an admission profile; check yours with
`reverpi doctor --provider configs/provider.local.yaml --config configs/online-projection.example.yaml`
and see [configuration.md](configuration.md#online-projection-admission).

## 5. Read the results

```bash
reverpi costs --run runs/demo                     # read-only ledger summary; creates nothing
reverpi costs --run runs/demo --cell demo-1       # one session only
reverpi reconcile --run runs/demo --attempt 3 --tokens 8123 --usd 0.0041 --evidence invoice.json
```

A gateway run directory holds `sessions.sqlite`, `ledger.sqlite` and `archive.sqlite` (plus
`projection.sqlite` when online projection is on); the Pi run directory (`runs/demo/pi`) holds
`task.json`, `events.jsonl` and `session.jsonl`. `costs` fails with `ledger_missing` for a mistyped
path instead of reporting zero. When a provider bill disagrees with the ledger, `reconcile` records
the correction against the specific attempt and evidence reference instead of editing history; it
uses the ledger's own bound budget, so no study config is needed.

### Research study runs

`reverpi analyze --run STUDY_RUN` reads a *study* output directory written by `reverpi run`
(`manifest.json` plus result rows), not a gateway run directory, and needs no gateway:

<!-- doc-contract: research-path -->
```bash
reverpi fixtures --out data/fixtures --count 4    # SEARCH-only protocol fixtures, not a benchmark
reverpi run --provider configs/mock.chat.yaml --config configs/pilot.yaml \
  --public data/fixtures/public.jsonl --gold data/fixtures/gold.jsonl --out runs/study
reverpi analyze --run runs/study
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Pi dependencies missing` | Run `cd pi && npm ci`; the extension needs Pi's runtime, not just Node |
| `Pi changed the frozen reasoning effort` | Pi's thinking level differed from the session's; align `--thinking` with the provider config's `effort` |
| `ledger_missing` from `costs` | The `--run` path has no ledger (often a typo); nothing was created |
| `online_profile` from the gateway | Online projection is not admitted for this Provider; run `reverpi doctor --provider P --config S` for the exact constraints |
| `Fresh task requires a new gateway session` | The session was reused after attempts were recorded; create a new session and output directory |
| `Real Provider calls require --allow-paid` | Real dispatch is opt-in on the `gateway` (and `run`/`probe`/`review`); pass `--allow-paid` there deliberately. `pi-run` has no such flag |
| Ledger shows `unknown_attempts > 0` | An attempt was interrupted after dispatch; it is reported as unknown rather than assumed free — reconcile it with provider evidence |
| Research suites error with a missing module | Those suites need fixtures that ship only with the private research archive; use `sh scripts/run_local_checks.sh` for the repository's own contracts |
