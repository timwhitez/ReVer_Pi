# Contributing

## Before you change anything

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
cd pi && npm ci && cd ..
sh scripts/run_local_checks.sh          # contract tests, plus the full suites when fixtures exist
sh scripts/offline-smoke.sh /tmp/rever-smoke
```

Both must pass before a change is proposed. There is no CI workflow in this repository: verification
is local, and a change is only as good as the checks you ran and reported.

## Rules

- **Never patch Pi.** Use its public extension API. If a change needs Pi internals, open an issue
  instead of vendoring a fork.
- **Keep research and product separated.** Product code lives in `src/reverpi/`, `pi/`, `configs/`,
  `docs/`; the historical record lives in `research/`. Do not add new experiment results to the
  product tree.
- **Preserve evidence semantics.** Anything that shortens or rewrites context must keep the original
  retrievable by exact handle, and must label excerpts as excerpts.
- **Fail closed.** New behaviour that cannot verify its inputs stops rather than guessing. Unknown
  cost, unknown usage and unknown provenance stay unknown.
- **No credentials, ever.** Keys come from the environment; config files, tests and artifacts must
  never contain them, and private endpoints are not committed.
- **One bounded check per behaviour change.** Add the smallest test that fails if the logic breaks;
  put fixture-free tests under `tests/` and run them via `scripts/run_local_checks.sh`.

## Reporting a change

State what changed, the exact commands you ran with their exit codes, and any assumption the reader
must hold. Do not describe mock or scripted results as model-quality evidence.
