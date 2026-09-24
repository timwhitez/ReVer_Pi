# Research record

The experiment record that produced this codebase lives in [`research/`](../research/README.md):
S3 through S5B plans, audits, runners, fixtures and the manuscript. It is a historical record, not
part of the installable product, and it is deliberately excluded from the default checks.

## What the record does and does not establish

Two findings matter to anyone reading this repository as a potential user:

1. **Completed-pair analysis hid bounded failure.** Re-deriving outcomes for every submitted run
   that reached a sealed boundary (33 runs) showed 11 censored runs and one confirmed harm, where
   the selected arm exhausted its suffix cap while the alternative arm answered correctly.
2. **The fitted projection threshold did not beat doing nothing.** On the 17 runs where both arms
   actually ran, the frozen rule cost more tokens than always-full (1,329,748 vs 1,313,508) with the
   same failure count, and the observed optimum was one sample wide (leave-one-source-out reproduced
   it in 1 of 14 folds).

Consequences that are enforced in the product:

- Projection is **opt-in and off by default**; no auto-projection policy is shipped.
- The old `deployable=true` flag from the research record is **not** treated as permission anywhere in
  the code; a new run needs an independently bound gate or it stays on `full`.
- Mock runs are labelled protocol checks, never quality evidence, and dollar figures are reported as
  unknown when no provider price is configured.

The repository therefore ships mechanisms and contracts — archive-backed exact recall, the ledger,
session capabilities, verifier receipts, fail-closed budgets — rather than a claim that masking saves
tokens or improves outcomes.
