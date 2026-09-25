# Configuration

All configuration is plain YAML or JSON, validated by strict Pydantic models in
`src/reverpi/config.py`; unknown keys are rejected rather than ignored.

## Provider (`configs/provider.*.yaml`)

| Field | Meaning |
|---|---|
| `name` | Label recorded in artifacts |
| `protocol` | `chat_completions` or `responses` |
| `base_url` | Provider API root, e.g. `https://api.deepseek.com` |
| `api_key_env` | Environment variable holding the key; the key itself is never stored |
| `model` | Model identifier sent in the request |
| `effort` / `effort_map` | Reasoning level and how it is named on the wire |
| `reasoning_style` | `standard`, `thinking`, or `thinking_only` provider dialect |
| `chat_output_field` | `max_completion_tokens` (default) or `max_tokens` |
| `max_output_tokens`, `context_window` | Hard model limits used for admission |
| `context_admission` | `upstream` (provider rejects) or `byte_bound` (client-side pre-check) |
| `stream`, `stream_options_usage` | Transport shape; usage reporting is required for accounting |
| `supports_native_compaction` | Whether the provider exposes its own compaction primitive |
| `requests_per_minute`, `tokens_per_minute`, `concurrency` | Client-side pacing limits |
| `prices` | Optional per-million input/cached/output price; if `configured: false`, currency stays unknown |
| `retry` | Bounded retry policy; `retry_ambiguous: false` means an interrupted request is never replayed |

Examples: `configs/provider.chat.example.yaml`, `configs/provider.responses.example.yaml`,
`configs/provider.thinking_only.example.yaml`, `configs/flash/provider.mock.json`.

## Study (`configs/pilot.yaml`, `configs/smoke-all.yaml`, …)

| Field | Meaning |
|---|---|
| `name`, `split`, `repeats`, `seed` | Study identity; `split` gates held-out analysis in the CLI |
| `methods` | Context strategies enabled for this study |
| `compression` | Sizing for the archive-backed memory |
| `budget` | Token, attempt, per-cell and disk limits |
| `actor_max_turns` | Turn ceiling for the whole task |
| `online_projection` | Request-boundary projection settings |
| `early_response_headers`, `response_heartbeat_seconds` | Transport behaviour for restricted networks |

### `compression`

| Field | Default | Meaning |
|---|---:|---|
| `memory_bytes` | 18000 | Active memory budget |
| `recent_records` | 4 | Records always kept verbatim |
| `excerpt_chars` | 400 | Excerpt size left when an observation is masked |
| `archive_bytes` | 20000000 | Archive capacity |
| `recovery_calls` | 3 | Maximum recovery tool calls per task |
| `recovery_chars` | 6000 | Maximum characters per recovery read |
| `recovery_search_mode` | `head` | `match` enables literal navigation before reading |

### `online_projection`

| Field | Default | Meaning |
|---|---:|---|
| `mode` | `off` | `off`, `observe` (record decisions only) or `apply` |
| `recovery_interface` | `legacy` | `split_v1` enables separate `search_evidence` / `recover_evidence` tools |
| `min_observation_bytes` | 10240 | Only observations at least this large are candidates |
| `excerpt_bytes` | 1024 | Bytes retained inline; must be smaller than the threshold |
| `full_exposures` | 2 | Times an observation stays complete before masking |
| `keep_recent_results` | 1 | Most recent results never masked |
| `admission` | `research_profile` | Who vouches for a live run: `research_profile` or `operator_declared` (below) |

### Online projection admission

`mode: off` is always admitted, and the protocol-only `mock` Provider is admitted for local checks.
A **live** Provider with `mode: observe` or `apply` must pass one of two admission profiles, checked
before any file is created or request is sent:

| Profile | Admits | Status |
|---|---|---|
| `research_profile` (default) | `model` ∈ {`deepseek-flash`, `gpt-6-luna`}, `effort: low`, `concurrency: 1` | The only combinations exercised by the recorded research runs. Changing any of them is refused, and so is reusing an old plan under a new identity |
| `operator_declared` | Any `model`, with `effort: low`, `concurrency: 1` and a required `expected_response_model` that the ledger enforces on every response | **Configurable but not certified.** You vouch for the route; no compatibility, quality or savings claim follows |

A model name is an identity, not a capability: renaming a local model to a research label does not
make it the research model. Check a configuration without any network access:

```bash
reverpi doctor --provider configs/provider.local.yaml --config configs/pilot.yaml
```

The `online_projection` block reports `allowed`, `certified`, `basis`, and a `problems` list naming
each unmet constraint. The gateway refuses with the same list (`online_profile`).

## Methods

| Method | Behaviour |
|---|---|
| `full` | Uncompressed reference: nothing is masked |
| `tail` | Recent window with goals/constraints protected |
| `mask` | Old observations masked to excerpt + handle, recent records verbatim |
| `archive` | Content-addressed handle/excerpt compaction |
| `lexical` | Query-known selection using only current task text |
| `summary` | Structured model-written summary |
| `hybrid` | Deterministic masking, then summary if still over budget |
| `observations` | Incremental observation memory |
| `type_guard` | Literal goal/constraint protection plus summary |
| `rever_lite` | Obligation-prioritised extractive selection |
| `rever_summary` | ReVer cards with a synopsis replacing low-priority text |
| `validity_only` | Archive baseline with the same annotation, no selection |
| `pi_original`, `pi_native` | Pi's own compaction, without and with the same recovery capability |

## Gateway and session limits

```bash
reverpi gateway --provider CONFIG --config STUDY --out RUN_DIR \
  --host 127.0.0.1 --port 8765 [--allow-paid] [--allow-network]
reverpi session-create --run RUN_DIR --id ID --method METHOD --token-file FILE [--ttl SECONDS]
reverpi session-revoke --run RUN_DIR --id ID
```

Session TTL is bounded to 1 minute … 7 days. The token file is created exclusively and the token is
never printed.

## Extension environment variables

| Variable | Required | Meaning |
|---|---|---|
| `REVER_GATEWAY_URL` | yes (set by `reverpi pi-run`) | Gateway base URL |
| `REVER_SESSION_TOKEN` | yes (set by `reverpi pi-run`) | Session capability token |
| `REVER_MAX_TOOLS`, `REVER_MAX_TURNS` | no | Turn/tool ceilings enforced by the extension |
| `REVER_PAIRED_WORKSPACE`, `REVER_PAIRED_FILES` | research read-only mode | Workspace allowlist for evidence tools |

Revalidation (opt-in; all required together):

| Variable | Meaning |
|---|---|
| `REVER_ENABLE_REVALIDATION=1` | Turns the tool on |
| `REVER_VERIFIER_REGISTRY`, `REVER_VERIFIER_REGISTRY_SHA256` | Pinned verifier registry and its digest |
| `REVER_VERIFIER_RUNNER`, `REVER_VERIFIER_RUNNER_SHA256` | Pinned runner and its digest |
| `REVER_VERIFIER_PYTHON` | Interpreter used for verifier calls |
| `REVER_MAX_REVALIDATIONS` | Per-task cap |

A missing digest, a changed runner or a reused receipt fails closed.
