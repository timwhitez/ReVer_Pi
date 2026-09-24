# ReVer-Pi

**Evidence-preserving context projection for Pi agents.**

ReVer-Pi is a standalone extension for [Pi](https://github.com/earendil-works/pi) plus a local
control-plane gateway. Observations leave the active context as exact, addressable evidence
instead of disappearing: the agent can search the archive and re-read any interval byte-for-byte,
while the gateway keeps one central ledger, enforces a frozen budget, and never invents a number
it did not observe.

> [!IMPORTANT]
> This repository is a working codebase, not a results claim. It is not an official distribution
> of Pi, it is not affiliated with SoL-Pi, and it does not claim to outperform either of them.
> The experiment record that produced the design lives in [`research/`](research/README.md) and is
> not required to install or run the agent.

## TL;DR

```bash
pip install -e '.[test]'    # gateway + CLI (+ pytest for the local checks)
cd pi && npm ci && cd ..    # Pi extension dependencies
sh scripts/offline-smoke.sh # end-to-end check: gateway -> session -> Pi -> ledger, no credentials
```

The smoke run starts a loopback gateway with a protocol-only mock model, opens one session, drives
Pi through the extension, and prints the ledger. It makes no external request and needs no API key.
A real provider is a separate, explicitly acknowledged configuration step.

## What ReVer-Pi adds

| Area | Mechanism | What changes |
|---|---|---|
| Context | **Online projection** | Tool observations above a size threshold, seen more than the configured exposures, are replaced at a request boundary by a bounded excerpt plus a content-addressed handle. Default mode is `off`. |
| Evidence | **Archive and exact recall** | `search_evidence(query)` locates a literal in archived text; `recover_evidence(handle, start, chars)` returns the exact interval. Reads never claim to revalidate current workspace state. |
| Verification | **Revalidation receipts** (opt-in) | An operator registers a pinned verifier and runner; the agent can request a fresh run and receives a receipt bound to the current task, workspace and revision. Nothing is auto-run and no model-selected command is accepted. |
| Accounting | **One central ledger** | Every attempt is recorded with its usage and currency status. Unknown attempts stay unknown instead of becoming zero, and `reverpi costs` / `reconcile` report from that single source. |
| Budgets | **Frozen study config** | Token, attempt, per-cell and disk limits are bound to a study config; the gateway fails closed rather than silently exceeding them. |

Four rules hold across all of them:

- **No Pi patches.** The extension uses Pi's public extension API and a pinned Pi release.
- **Explicit opt-in.** Projection defaults to `off`; recovery tools register only when the session
  grants recovery capability.
- **Evidence is never rewritten.** Archived bytes are stored once and retrieved exactly; excerpts
  are labelled as excerpts.
- **Honest accounting.** Mock usage is synthetic and labelled; partial, unknown and unreconciled
  costs are reported as such.

## Requirements

- Python 3.11+ with the dependencies in `pyproject.toml` (`pip install -e .`), pinned by `requirements.lock`
- Node.js 22.19+ and Pi `@earendil-works/pi-coding-agent@0.84.2` (installed by `cd pi && npm ci`)

## Documentation

| Document | Contents |
|---|---|
| [docs/getting-started.md](docs/getting-started.md) | Install, offline smoke, first real-provider run, reading results |
| [docs/configuration.md](docs/configuration.md) | Provider, study and compression configuration; environment variables; methods |
| [docs/architecture.md](docs/architecture.md) | Gateway, sessions, projection, archive, ledger, failure behaviour |
| [docs/compatibility.md](docs/compatibility.md) | Pinned versions and compatibility rules |
| [docs/research.md](docs/research.md) | Where the experiment record lives and what it does and does not establish |
| [agents-install.md](agents-install.md) | Procedure for coding agents installing or verifying this repository |

## Safety and scope

- The gateway binds to loopback by default; non-loopback binding requires an explicit flag and is
  the operator's responsibility to isolate.
- Real provider calls require `--allow-paid` and an explicit acknowledgement; credentials are only
  ever read from environment variables and are never written to artifacts.
- The evidence tools are for evidence-grounded work; they are not a sandbox for untrusted code.
- Reusing this repository's research numbers as a product claim is out of scope: see
  [docs/research.md](docs/research.md).

## License

MIT — see [LICENSE](LICENSE).
