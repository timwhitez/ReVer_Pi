# ReVerPi

Pi-based agent and source-separated context-management research.

## Status

S5 imports the complete current **implementation source**, not the private experiment archive or historical regression fixtures. New risk receipts are required before the deployed-mode runner can choose projection. The old 30% flag does not authorize the supplied 5% plan. No claim of superiority or new paid allowance is made.

The production Python/Pi sources remain the frozen RC7.1 lineage. S5 adds strict scoring, censoring-aware assessment and research-layer runtime binding. Read `docs/s5/RESEARCH_PLAN.md` and `paper/s5/manuscript.md`.

## Development

Use Python 3.12+ (the local validation used 3.13.5), Node 22.19+ and the supplied dependency locks. The Pi version is fixed to 0.84.2; do not silently substitute SoL-Pi's newer requirement.

`python -m pip install -e .` installs the Python implementation. `cd pi && npm ci && npm run typecheck` installs/checks the Pi extension. Do not provide model secrets to any test run.

Standalone assurance checks:

```bash
python -B -m pytest research/s5/tests/test_s5_contracts.py research/s5/tests/test_s5_statistics.py research/s5/tests/test_s5_assurance.py -q
```

The full private S5 delivery supplies fixed development source snapshots and historical regression fixtures for all tests. They are deliberately not copied into this import. Full-repository pytest without those fixtures is not the local contract either. `research/s5/run.py census` requires the original S4 delivery in a separate local directory. It does not fetch evidence from GitHub or call a model.

Verification is local-only; this repository has no CI workflow:

```bash
sh scripts/run_local_checks.sh
```

It runs the pytest suites present in the checkout and the Pi typecheck/tests when `pi/node_modules` exists, prints skip lines when it does not, and exits non-zero on any failure.

## Evidence and permissions

Raw requests, responses, ledgers, proxy addresses, model credentials and runtime binaries are excluded. A code/manifest digest is not an independent reviewer or a proof of sampling assumptions. Keep the repository private until a separate publication review. The independent CCA research track is outside this project.

## Import lineage

This branch has the existing main commit b2333898e75f317b78ddee57335448b724df741a as its parent. It is delivered as a Git bundle for an authorized host; creating the bundle does not mean it has been pushed. No force push is required.
