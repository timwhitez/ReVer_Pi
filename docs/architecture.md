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

APIClient sends all request-path ledger work, including provider binding and transport audit,
through one private worker with at most 32 admitted jobs (running plus queued). Queue backpressure,
the operation lock, claim, rate admission and HTTP work share the original `retry.total_seconds`
deadline. Each job opens and closes its own connection on the worker. SQLite lock admission waits
in slices of at most 50ms; cancellation before COMMIT rolls back, while an already committed
result stays attached to its original operation/attempt. Cancelling one request does not stop
another request's ledger work. The synchronous CLI retains its existing Ledger interface.

Accounting cleanup has an explicit two-second allowance per attempt: committed usage remains
accounted through caller cancellation, and settlement, response-model accounting, trace and final
operation persistence share one absolute cleanup deadline. Cancellation does not reset it. Only
a fully reconciled attempt that permits a retry starts a new accounting allowance; ordinary retry
work retains the original execution deadline. If that execution deadline expires during accounting,
the request completes bounded cleanup and cannot dispatch another attempt. A caller can therefore
take up to the cleanup allowance beyond `retry.total_seconds` while durable cleanup completes.
Python callers may set
`ledger_max_pending` and `ledger_cleanup_seconds` on APIClient; these are not provider YAML fields.

When cleanup cannot complete, `ledger_cleanup_timeout` reports an ambiguous outcome. A cancelled
caller still receives cancellation; this does not certify ledger reconciliation. Durable running
operations remain `operation_in_doubt` on restart, and reserved/unknown attempts remain charged at
their planning bound until explicit operator reconciliation. No claim or paid attempt is replayed.
Use APIClient as an async context manager or await `close()` on its owning event loop. Close rejects
new requests, cancels ordinary in-flight work, waits for owned accounting and actual transaction
outcomes, joins the worker, then closes HTTP resources. A cancelled close waiter may await close
again; its retained close task continues draining. Gateway lifespan performs this shutdown.

The same absolute deadline bounds admission and waiting for an already submitted job, including
a cleanup job queued behind another operation's SQLite lock wait. Expiry signals only that job;
its actual future and received usage arguments stay owned by the bridge until the worker reports
the real outcome. A queued expired job performs no writes. Received usage that could not be
committed within the allowance remains conservatively reserved/unknown, with the operation in
doubt; it is not reported as durable known usage. Recovery still needs external billing evidence
and explicit reconciliation. A COMMIT that won the timeout race keeps its durable result; close
waits for the actual future and joins even when the caller already received an ambiguous error.

Request-path model observation and a model-drift stop share one SQLite transaction. The stop
commits before the worker can process another queued reservation, and the drift error is raised
after COMMIT. An aborted transaction never certifies that a stop was persisted. Existing requests
already dispatched keep their usage records; stopping blocks new paid admission.

SSE model changes are detected by the parser, before a model-observation transaction exists.
The client immediately latches drift and checks this latch before reservation and again after
an awaited reservation, so admitted but unsent work cannot dispatch while stop persistence is
queued. The latch is set before HTTP stream close can await or be cancelled. A stop transaction
then takes priority over trace/settlement within the same attempt's cleanup allowance, including
when stream close was cancelled. Atomic observation drift does not enqueue another stop.
If storage prevents the parser stop from committing in time, the caller receives an ambiguous
cleanup error, the incomplete response keeps reserved/unknown usage, and that client continues
to refuse new dispatch. This local latch does not certify a durable stop for other clients or a
restart; only the actual committed ledger stop does. Already dispatched requests retain their
accounting and are not recalled.

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

Native print tasks retain their exact UTF-8 text in `prompt.txt` and pass a
  one-shot JSON envelope through stdin. The extension's opted-in public input
  hook restores the text without Pi's `@file` path header or stdin whitespace
  trimming. Harbor uses the same transport; RPC acceptance remains ordinary RPC.
  This removes unsolicited task-file metadata, not filesystem access. Cwd and
  credential scrubbing do not isolate neighboring files or provide a sandbox.

## What this is not

- Not a sandbox: tools run with the privileges of the Pi process.
- Not a benchmark harness: research runners live under `research/` and are not installed.
- Not an authority on model quality: mock transports are scripted and labelled as such.
