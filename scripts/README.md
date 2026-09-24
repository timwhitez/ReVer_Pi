# Scripts

Operational scripts for the ReVer-Pi gateway and for the research record under `research/`.
They run locally; there is no CI in this repository.

## Product entry points

| Script | Purpose |
|---|---|
| `run_local_checks.sh` | Verification entry point: CLI environment report, fixture-free assurance contracts, and the fuller suites when fixtures and `pi/node_modules` exist |
| `offline-smoke.sh` | End-to-end offline check: loopback gateway with the mock provider, one session, Pi through the extension, ledger totals |
| `verify_offline.py` | Bounded zero-model verification profile with an egress guard (needs a C compiler) |
| `verify_rc7.py`, `verify_rc71.py`, `verify_rc6.py`, `verify_release.py` | Release-lineage verification for the sealed research packages |
| `bootstrap.sh`, `capture_environment.py` | Environment capture helpers |

## Research helpers

The remaining scripts (`audit_*.py`, `*_smoke.py`, `*_tamper.py`, `paired_*`, `intervention_*`,
`flash_workflow.py`, `net_client.mjs`, `replay_activation.mjs`, …) belong to the study runners in
`research/`. Some are imported by `research/s4/reverpi_sources/runner.py`, which is why they live
here rather than under `research/`; they are not part of the installed package.
