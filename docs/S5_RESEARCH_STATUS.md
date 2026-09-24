# S5 research status — 24 September 2026

## What was independently reconstructed

The supplied S4 archive has SHA-256 `2b57e3188492a6b852684e8a85a19b92db96e7442fbb825a891ae762cf22f29c`. Its 12,980 listed files match. Across 86 submitted runs, 641 recorded completed model requests account for 8,202,832 reported tokens with no unknown attempts. Private billing and backend weights are not independently certified.

The paired/capture subset has 76 runs: 44 no-eligible, 17 stopped, 15 complete pairs. Only 27 runs have an actual sealed boundary consistent with the capture endpoint; the reported 32 includes five capture-stage stops. Ten single-arm integration runs have six actual boundaries, not seven.

Historical extracted-answer scoring gives 11 both-correct, two both-incorrect, one full-only and one projected-only among the 15 completed pairs. These are selected completed outcomes, not an unbiased quality estimate. All historical records remain unchanged.

## Why the old deployable flag is not accepted

The fitted rule projects when `history_bytes >= 93641`. The submitted final calibration reports zero selected harms among nine completed-case source groups, giving a numerically correct fixed-n one-sided upper bound of 0.2831288356. The delivered authorization, however, permits 0.05 and binds an earlier dataset digest; the final object uses 0.30 and different data. Additional approval outside the supplied archive is unknown.

A complete-pair filter also omits `pathspec-util`: history_bytes=94493 selects projected; full answers correctly in three suffix requests; projected exhausts its frozen twelve suffix requests without a final answer. Full logical completion usage is 123,741 tokens; projected partial usage is 277,168, not completed-task cost. This is an observed adverse completion-at-cap result, not an incorrect final string.

A post hoc frame of all 22 observed eligible nontraining runs has 17 source labels, one known selected harmful group and three unresolved groups. Its finite-frame identification interval is [1/17,4/17]. This is NOT a confidence interval, prospective calibration or population risk estimate.

## Changes in this PR

- `strict_object_v2` rejects duplicate keys and nonfinite values before normalization. A whole JSON fence is accepted; arbitrary inner fragments/prose are not. This new scoring contract never replaces historical scores.
- Complete source/task frames preserve unknown outcomes. Actions are recomputed from frozen rules and pre-action features.
- Permission binds independently supplied design and authorization digests, candidate, model, scorer and runtime. Missing permission defaults to full; mismatches are errors.
- Fixed-n and conservative alpha-spending bounds are separate. Hashes do not prove independent sampling or chronology. Risk receipts never authorize paid requests or monetary claims.

This first GitHub PR contains the standalone assurance layer, its core tests and CI. The complete agent runtime, imported S4c integration, private historical fixtures and fuller census are delivered separately in the S5 source archive; they have NOT all been imported into this branch. Raw private traces, proxy configuration and credentials are intentionally absent.

## Next research gate

Do not rerun old successful tasks to rescue a deployment claim. Freeze one candidate, scoring contract, full frame, model/protocol, budget endpoint and stopping rule before new outcomes. Include no-eligible, cap-stopped and unresolved cases. Use source-disjoint calibration only under a justified sampling design. Do not silently loosen a margin or repeatedly inspect fixed-n bounds until one passes.

New provider experiments require separate explicit authorization. Current native/container testing of the changed deployment entrypoint remains unexecuted. The existing threshold is a research candidate, not a proven improvement over Pi. The separate CCA paper remains out of scope.

## Primary literature

- SoL-Pi: https://github.com/NVlabs/SoL-Pi ; arXiv:2609.20519. Archiving/recall and capability-constrained harness research are established mechanisms.
- The Complexity Trap, arXiv:2508.21433v3. Masking/hybrid baselines must not be presented as new inventions.
- ACON, arXiv:2510.00615v3, and TRACE, arXiv:2608.06503. Feedback optimization and matched continuation motivate rigorous comparisons.
- Bates et al., Distribution-Free, Risk-Controlling Prediction Sets, arXiv:2101.02703v3. Calibration assumptions matter.
- Howard et al., Time-uniform, nonparametric, nonasymptotic confidence sequences, arXiv:1810.08240v9. Our union-bound alpha spending is a simpler conservative method, not their efficient construction.
