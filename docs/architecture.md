# Architecture

```
Pi (unmodified, pinned release)
  └── ReVer-Pi extension  (pi/src/index.ts)
        │  REVER_GATEWAY_URL + REVER_SESSION_TOKEN
        ▼
  ReVer-Pi gateway  (src/reverpi/gateway.py, FastAPI, loopback)
        ├── sessions.sqlite      session capability, method, TTL, compaction claims
        ├── archive.sqlite       content-addressed exact evidence + excerpts
        ├── projection.sqlite    request-boundary projection state (opt-in)
        └── ledger.sqlite        one row per attempt: usage, currency, unknowns
              │
              ▼
        Provider HTTP API (real, or the scripted mock transport)
```

## Components

**Extension (`pi/`)** registers a Pi provider that points at the local gateway, forwards canonical
messages and tool definitions, and exposes only the evidence tools the session is entitled to. It
owns the turn and tool limits, aborts on limit breach, cancels session fork/switch, and leaves Pi's
own compaction path available when the study config says so.

**Gateway (`src/reverpi/gateway.py`)** is the only component that talks to the provider. It holds the
session capability, applies the configured context strategy, archives evidence, writes the ledger and
streams responses back. Endpoints: `/health`, `/session`, `/complete`, `/compact`, `/recover`.

**Context strategies (`src/reverpi/memory.py`, `src/reverpi/online_projection.py`)** implement the
named methods (`full`, `mask`, `archive`, `rever_lite`, …). Masking replaces an old observation with a
bounded excerpt plus a handle; the archive keeps the original bytes, and the handle is the SHA-256 of
the archived content, so a reader cannot confuse a summary with the source.

**Archive** stores archived payloads and answers literal searches with handle/excerpt pairs.
`recover_evidence` returns an exact codepoint interval; `search_evidence` is ASCII case-insensitive
and bounded, so a search cannot silently return rewritten text.

**Ledger (`src/reverpi/ledger.py`)** records every attempt before dispatch and completes it after.
Unknown currency, interrupted attempts and reconciled corrections are separate states; totals are
derived from the rows rather than accumulated in a variable that can drift.

**Budgets (`src/reverpi/config.py`)** bind token, attempt, per-cell and disk limits to a study config.
The gateway refuses to start a dispatch that would breach them.

**Revalidation (`pi/src/revalidation-extension.ts`, `src/reverpi/revalidation.py`)** is opt-in. The
operator supplies a pinned verifier registry, a runner and their SHA-256 digests; the agent can then
request a fresh run and receives a receipt bound to the current task, workspace and revision. A stale
receipt cannot mint a new one.

## Request flow

1. Pi sends a completion request to the gateway with the session token.
2. The gateway authenticates, checks the session is fresh and within budget, then builds the message
   list for the configured method: recent records verbatim, old observations replaced by excerpt +
   handle, protected records (goals, constraints) untouched.
3. The provider call is recorded in the ledger, dispatched, and its usage parsed from the provider's
   own report.
4. The response returns through the same path; Pi executes any tool calls in the workspace.
5. Tool results re-enter on the next request, where new observations may be archived and masked.

## Failure behaviour

- **Fail closed.** Limit breach, unknown method, stale session, mismatched digests and provider errors
  stop the turn instead of being reinterpreted as success.
- **No silent retry.** An interrupted response may already have been billed, so the gateway does not
  replay it; the attempt is recorded and the run stops for operator reconciliation.
- **No invented numbers.** If a provider omits usage the attempt is `unknown`, and totals report that
  separately rather than treating it as zero.
- **No evidence rewriting.** Archive reads are exact and excerpts are labelled; a summarising method
  is a different method, not a different view of the same bytes.

## What this is not

- Not a sandbox: tools run with the privileges of the Pi process.
- Not a benchmark harness: research runners live under `research/` and are not installed.
- Not an authority on model quality: mock transports are scripted and labelled as such.
