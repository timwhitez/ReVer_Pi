# Research record

This directory is the historical record of the S3–S5B research programme that produced the
ReVer-Pi extension and gateway. **It is not part of the installable product** and none of it is
required to use the agent. Nothing here is a product claim.

| Path | Contents |
|---|---|
| `s3/`, `s4/` | Study runners, task/source snapshots contracts, extracted analysis adapters |
| `s4c/` | S4c adapter: data-root override, scoring contracts, deployed-mode single-arm runner |
| `s5/` | Scoring (`strict_object_v2`), uncertainty bounds, censoring-aware census, deployment-permission contracts |
| `s5b/` | Full-denominator re-analysis of the S4 campaign (33 boundary runs, threshold surface, leave-one-source-out) |
| `docs/s5/` | Research plan, audit report, local-agent handoff prompt, GitHub import notes |
| `paper/s5/` | Manuscript draft |
| `configs/` | Study-specific configuration used by the research runs (not product defaults) |

## Reproducing the checks that do not need private fixtures

```bash
sh scripts/run_local_checks.sh
```

The S3/S4/S4c suites need development source snapshots and raw-evidence fixtures that are not
distributed; `run_local_checks.sh` runs the fixture-free assurance contracts always and adds the
rest only when those fixtures are present.

## Facts this record establishes

- 641 completed provider requests were audited across 86 submitted runs, with 8,202,832 reported
  tokens and no unknown attempts in that archive. Private billing and backend model identity were
  never verified.
- The complete-pair view hid bounded failure: of 33 runs that reached a sealed boundary, 11 were
  censored, one (`pathspec-util`) is a confirmed harm where the selected arm exhausted its suffix
  cap while the alternative arm answered correctly.
- On the 17 runs where both arms were dispatched, the fitted projection threshold cost more tokens
  than always-full (1,329,748 vs 1,313,508) with the same failure count.
- `strict_object_v2` closes a duplicate-key scoring bypass; historical scores were never rewritten.

## Facts this record does not establish

- No dollar figure: provider prices were unconfigured, so currency stays unknown.
- No population estimate, no unbiased quality comparison, no non-inferiority result.
- No deployment permission: the historical `deployable=true` flag is rejected by design, and the
  gate contracts require an independently bound authorization.
