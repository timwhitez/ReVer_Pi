# ReVerPi: Censoring-Aware Evaluation of Selective Context Management

**Research manuscript S5 | 24 September 2026 | Authors and affiliations to be supplied**

## Abstract

Selective context management aims to reduce agent resource use while retaining task capability. A successful compressed continuation, however, does not establish that a learned selection rule is safe to deploy. We investigate this distinction in a Pi-based, source-reading research system using a submitted campaign with 641 completed model requests and 8,202,832 reported tokens. A census of 76 paired-or-capture runs finds 27 realized eligibility boundaries and 15 completed pairs; ten additional runs exercise single-arm integration. This corrects a boundary count that included capture-stage stops. A threshold trained on four source groups was subsequently marked deployable using zero observed selected harms in nine complete-case calibration groups. The numerical upper bound is reproducible, but the supplied authorization permits a different risk margin and dataset. Moreover, a run excluded by the complete-pair filter is selected for projection, while its full branch answers correctly and its projected branch exhausts twelve suffix requests without an answer. We preserve historical scores and introduce a versioned strict answer contract, complete-frame outcome accounting, and an independently bound permission receipt. These changes do not establish population noninferiority or cost superiority. They turn deployment assertions into inspectable conditions and expose a concrete interaction between adaptive task construction, censoring, and selective context policies. The release includes the current agent and research code, a reproducible census, negative tests, and a prospective evaluation design; no new model requests were made for this revision.

## 1. Introduction

Context management is useful only when a reasoning agent still completes the work that matters. Retaining an observation in an archive is insufficient if the agent cannot discover the relevant passage, construct a valid request, or afford another reasoning turn. Conversely, a short continuation that stops without an answer should not be treated as inexpensive completion. The evaluation problem becomes harder when a selection rule decides which histories to compress. In that setting, the reported outcome can depend on which tasks become eligible, which branches finish, and which outcomes are admitted to calibration.

ReVerPi is a research system built around Pi. It keeps the complete recorded history, projects eligible old observations in outgoing requests, and supports bounded archive search and exact reading. A separate public revalidation operation can obtain a fresh observation about a workspace. These mechanisms are not claimed as inventions. The present work asks how their use can be evaluated and permitted without confusing a successful trace, a conditional estimate, and an authorized deployment decision.

Earlier project experiments included both non-activation and a successful single-prefix result. In one audited exploratory pair, the full and projected branches both answered correctly, with complete logical usage of 113,462 and 86,162 tokens respectively. The projected branch used an additional model request and obtained the required historical passage through search. That remains a useful positive case, not evidence that every eligible history should be projected. The latest supplied campaign expands the experimental record to many source-reading tasks and a trained one-threshold selector. It also exposes an evaluation failure that the single example could not reveal.

We make three limited contributions. First, we supply a request-level census that separates actual eligibility boundaries from stopped captures and separates completed pairs from incomplete outcomes. Second, we reproduce a selected adverse outcome omitted by complete-case calibration and a scorer contract that can discard duplicate keys before strict validation. Third, we implement a small assurance layer that binds the candidate, analysis design, scoring rule, runtime identity, and risk authorization while preserving unresolved outcomes. We do not present a new concentration theorem, a novel masking primitive, or a validated claim that ReVerPi outperforms Pi. The contribution is an executable empirical diagnosis and an improved basis for subsequent method research.

## 2. Related work and scope

SoL-Pi combines capability-constrained auto-research with reusable harness mechanisms, including observation storage and recall, context compaction, action fusion, and delegated reading [1]. Its public release makes mechanisms opt-in. ReVerPi's projection and archive interfaces belong to this established family. Comparisons must also control the underlying Pi version: this campaign uses Pi 0.84.2, whereas the retrieved SoL-Pi installation instructions specify 0.85.1. We do not present an uncontrolled cross-version comparison as a method effect.

The Complexity Trap systematically compares masking and model-generated summaries, including a hybrid strategy [2]. ACON optimizes compression guidance using feedback rather than requiring a separate hand-written decision rule for every task [3]. TRACE studies paired closed-loop continuations at shared environment states [4]. These studies motivate strong simple controls and matched continuations. The current source-reading adapter is restricted to read-only files, however, and is not a general snapshot mechanism for writable or networked environments.

Token Reduction Is Not Cost Reduction examines why cache behavior and additional interactions can change economic conclusions [5]. Accordingly, this paper separates reported token categories, request counts, and monetary costs. It does not assign a private proxy the pricing of a nominal public model. The earlier successful ReVerPi pair remains an example of fewer tokens accompanied by more interactions and different cache categories.

Risk-controlling prediction sets demonstrate the importance of calibrating a frozen procedure on suitable holdout data [6]. Confidence sequences provide simultaneous coverage over time rather than only at a fixed sample size [7]. Our implementation uses the simpler conservative allocation alpha_n = alpha / [n(n+1)] with ordinary one-sided binomial intervals. It is not an implementation of the efficient boundaries in [7], and it does not repair adaptive source construction or outcome-dependent missingness. The statistical tools are established; the empirical question is whether their assumptions match the artifacts being used to authorize an agent.

The design follows the general-method motivation of the Bitter Lesson [8]: the actor retains a small general tool vocabulary, and selection can be searched or learned from data rather than keyed to benchmark names. Identity checks and accounting contracts are infrastructure constraints, not a replacement for learned task reasoning. This revision deliberately does not fit a new threshold to the adverse case that it discovers.

## 3. System, candidate, and experimental units

### 3.1 Projection and evidence access

The base runtime preserves full history and changes eligible tool-result text only in outbound model requests. Eligibility uses observation size, completed full exposures, and recency/protection conditions. A completed exposure means the transport recorded a finished response after sending the observation; it does not prove the model attended to it. The split interface exposes archive search separately from exact interval reading. Search can return sufficient original text by itself, so an absent exact-read call is not automatically a recovery failure. Neither operation proves that old evidence remains applicable to a changed environment.

The experimental adapter exposes a fixed source subset and bounded read/search operations. Controller-side gold answers are hashed and scored separately. This reduces accidental answer leakage but is not an adversarial operating-system sandbox. The latest campaign contains denied or pending out-of-allowlist read attempts; their audit status is reported rather than silently promoted to successful interface compliance.

### 3.2 Shared-prefix evaluation

A capture stage uses full observations until the first eligible outbound request is prepared. It stops before dispatching that request and seals the completed response tape. Full and projected branches replay the same completed prefix and then continue from matched requests. At the first branch request, tool definitions and non-representation fields must match. Subsequent actions are free to diverge; such divergence is part of the measured continuation effect, not a reason to edit one branch.

A task that completes before eligibility has a no-eligible outcome. A capture that is stopped by a request or token condition without reaching a boundary is a different outcome. Counting both as evidence of activation would inflate coverage. The observation unit for the census is a submitted run. The inferential unit, if a future design justifies inference, may be an independently sampled source group. API requests, tool calls, random seeds, related tasks, and additional questions are not automatically independent units.

### 3.3 The submitted selector

The supplied search selects a one-feature threshold: project when history_bytes is at least 93,641 and otherwise keep full observations. The fit used four completed development pairs from four source labels, searched 36 candidates, and reported an equal-group mean token ratio of approximately 0.8473. This is a real fitted artifact rather than a hand-written task-name router. Its small development set, feature construction, and completed-outcome selection nevertheless constrain interpretation.

A subsequent calibration artifact contains eleven completed rows in nine source groups, with no selected harms. Only two of those eleven rows are actually selected for projection by the frozen threshold. A full decision is identically unharmful relative to the same matched full reference; therefore a low overall selected-harm rate can coexist with very little testing of projection. This is not mathematically wrong, but the population and denominator must be stated. It is not the probability of failure conditional on selecting projection.

## 4. Outcomes, censoring, and costs

Let Y_a(K) indicate a correct final answer by a frozen suffix-request allowance K for arm a. A branch that demonstrably uses all K requests without producing an answer has Y_a(K)=0, while its eventual result under a larger allowance is unknown. A transport interruption or a stage stopped before the specified endpoint is retained as unresolved unless a different endpoint was declared in advance. This descriptive reanalysis does not retroactively claim that every historical task used the same K or that a new endpoint was preregistered.

For a frozen selector pi(x), define a selected adverse event as full being correct and the selected projected branch failing at the specified endpoint. If pi(x) chooses full, the selected adverse event relative to that same reference is false by construction. If full is false or projected is true, harm is also ruled out. Other partially observed combinations remain unknown. The code implements this three-valued logic and rejects supplied action labels; selection is recomputed from the candidate and pre-action features.

For each declared source group, the source-any-harm endpoint is true if any declared task has a known selected harm, false if all declared tasks are known non-harmful, and unknown otherwise. The empirical lower count includes known harms, and the upper count includes both harms and unresolved groups. Dividing by the fixed frame size gives an identification interval for that finite frame, not a confidence interval for a population. Different source-any-harm task counts also require care: varying the number and difficulty of tasks changes the group endpoint itself.

Logical cost for an arm includes the shared capture plus that arm's suffix. Experimental acquisition cost includes capture once and both newly purchased suffixes. A replayed prefix is not a free deployment prefix. Input, output, and cached-input fields are not independently additive when cached input is a subset of input; reasoning tokens reported within output are likewise not added twice. Unknown usage is distinct from a zero-token operation rejected before an attempt. Monetary cost remains unknown without the relevant private prices or a bill.

## 5. Evidence reconstruction and census

The supplied delivery archive has SHA-256 2b57e3188492a6b852684e8a85a19b92db96e7442fbb825a891ae762cf22f29c. Its 12,980 listed members match. This establishes consistency with the submitted manifest, not an external timestamp or a cryptographic attestation of how the experiment was conducted.

We independently reconcile run summaries, stage records, sealed boundaries, raw request/response files, and SQLite attempts. The census finds 86 runs and 641 recorded completed requests, with 8,202,832 reported tokens and zero unknown attempts. All recorded responses carry the model label deepseek-flash. This authenticates the submitted labels and usage consistency, not the weights behind a private routing service. No new calls were made during reconstruction.

| Submitted run family | Runs | Actual boundaries | Complete pairs | Other endpoint information |
|---|---:|---:|---:|---|
| Paired or capture experiments | 76 | 27 | 15 | 44 no-eligible; 17 stopped |
| Single-arm integration | 10 | 6 | Not applicable | Three no-eligible; one capture stop |

The reported paired boundary count of 32 is reproduced by subtracting all no-eligible runs from 76, thereby treating five capture-stage stops as boundaries. Requiring both a sealed boundary and the corresponding stage endpoint yields 27. Similarly, the submitted single-arm count of seven becomes six. These are descriptive fractions of adaptively constructed submitted runs, not estimates of natural deployment coverage.

We reran the supplied auditor on all 76 paired-or-capture records. Seventy-three returned pass; 71 exactly matched a supplied audit object, and two had no prior audit object to compare. Three returned the original out-of-allowlist-intent error. Separate forensic inspection distinguishes explicit aborted results from pending calls without a result. It does not overwrite those three failures or certify a sandbox. Thus the independent accounting census and the stricter interface auditor answer different questions.

Among the fifteen completed pairs, the historical extracted-answer contract gives eleven both-correct, two both-incorrect, one full-only-correct, and one projected-only-correct pair. The completed-both-correct subset sums to 1,267,036 logical full tokens and 949,774 logical projected tokens. Yet its median per-pair projected/full ratio is approximately 1.292. A large saving on one task dominates the aggregate. Both descriptions must remain conditional on this selected subset; neither establishes general average savings or a complete quality comparison.

## 6. A censored selected adverse outcome

The decisive counterexample is pathspec-util. Its captured history has 94,493 bytes, above the frozen 93,641 threshold, so the candidate selects projection. Both branches share the audited prefix. The full branch answers correctly in three suffix requests. The projected branch uses all twelve permitted suffix requests and stops without a final answer.

| Stage | Newly dispatched requests | Reported tokens | Endpoint |
|---|---:|---:|---|
| Capture | 3 | 39,539 | Eligible boundary |
| Full suffix | 3 | 84,202 | Correct final answer |
| Projected suffix | 12 | 237,629 | Suffix cap; no final answer |

The full logical completion cost is 123,741 tokens. The projected branch has spent 277,168 tokens including capture, but its eventual completion cost is unknown. It is a known selected harm for completion by the frozen K=12 endpoint, not an observed incorrect final string. This run was excluded by a filter that only exports complete pairs, while another completed task from the same source label remained in calibration.

To expose the consequence without inventing a prospective sample, we construct a post hoc frame containing all 22 observed eligible runs outside the four training source labels. It covers seventeen source groups. One group has known selected harm, three remain unresolved, and thirteen have no selected harm under the available outcomes and frozen rule. The resulting finite-frame identification interval is [1/17, 4/17], or approximately [5.88%, 23.53%]. This is not a 95% interval, not an independent test set, and not a population risk estimate. It is sufficient to show that the zero-harm complete-case summary does not exhaust the evidence relevant to bounded completion.

The raw tool sequence clarifies the failure path without establishing a quota-removal counterfactual. After nine common-prefix tool calls, full performs five additional reads. Projected performs thirteen additional reads, two searches, and three exact-read requests. One exact read and both searches return archive payloads; the following two exact-read requests return the literal recovery-quota halt message. The frozen recovery-call allowance is three. These refusal payloads carry a false is_error field in the recorded transcript, so counting only that flag would miss them. The actor subsequently re-reads source fragments and reaches the suffix cap. Thus exact retrieval does occur in this campaign, but it is not sufficient for bounded task completion. We neither increase the old quota nor claim that increasing it alone would have produced a correct answer.

The example does not establish that history size is intrinsically a bad feature or that all projection strategies are harmful. It establishes that completion filtering can remove an adverse outcome exactly where a resource-saving policy acts. Retuning the threshold just above 94,493 after discovering this case would be development on an observed example, not a repair of calibration validity.

## 7. Why the deployment flag is not an authorization

For zero harmful groups in n=9 independent fixed-sample Bernoulli observations, the one-sided 95% binomial upper bound is 1 - 0.05^(1/9), approximately 0.28313. The supplied numerical value is correct. It passes an explicitly chosen 30% margin but not a 5% margin. The delivered authorization states 5% and binds an earlier calibration-data digest; the final object uses 30% and a different digest. We did not find an artifact authorizing that change in the submitted archive. We cannot infer whether additional approval existed outside it.

Several distinct questions therefore remain: whether the margin is authorized, whether the candidate and dataset match that authorization, whether the complete-case population is the intended target, and whether sampling and stopping justify the numerical coverage. Hash consistency answers none of the sampling questions. Sequentially adding tasks after seeing results or selecting source questions for exposure and completion can invalidate a fixed-sample interpretation even if the final calculation is reproduced exactly.

As a transparent alternative for a prospective fixed population with independent Bernoulli observations, the release implements alpha_n = alpha/[n(n+1)]. Since the allocations sum to alpha, a union bound supplies simultaneous coverage over the indexed intervals. At zero harms and n=9, this conservative upper value is approximately 0.56519. This calculation is illustrative: it does not retroactively legalize the campaign, eliminate informative censoring, or justify treating related sources as independent. A future study may use a more efficient validated confidence sequence [7], provided its actual conditions hold.

Ten single-arm integration runs show that the threshold can be evaluated and an arm dispatched. They do not supply within-state counterfactual outcomes. Of six realized boundaries, three choose full and three choose projected; five suffixes complete and one stops at its cap. These tests are relevant to wiring, not a substitute for risk evidence or task-quality comparison.

## 8. Implementation changes

### 8.1 Versioned scoring

The imported extracted-answer scorer searches for the first decodable object anywhere in final text and re-serializes it before strict scoring. A minimal witness is an object with the same key twice: the first value is wrong and the second matches gold. The decoder drops the first value, so the downstream strict scorer never sees the violation. A malformed outer object may also expose a decodable inner fragment. We reproduce the contract flaw without claiming that duplicate keys caused a particular observed success.

The new strict_object_v2 contract accepts one whole JSON object, optionally enclosed in one whole Markdown JSON fence. It rejects duplicate keys, non-finite or overflowing numbers, multiple objects, prose wrappers, and malformed nesting before normalization. Key sets and typed values are checked explicitly. This is a new scoring definition, not a transparent correction to historical scores. In sensitivity analysis, three historical final-answer labels differ because of formatting: the two click-parameters branches and the packaging-normalization capture. Main historical tables retain their declared contract.

### 8.2 Bound permission and conservative runtime behavior

A new assessment binds a candidate hash, full task frame, model fork, scorer identity, runtime identity, risk metric, inference method, and authorization. Source-overlap checks include the development frame. Observations must cover declared tasks; unresolved outcomes are not deleted. The selected action is recomputed from pre-action features rather than accepted from an export label. Preconditions include operator attestations about chronology, independent sampling, and outcome-independent selection. These attestations are visible assumptions, not facts proven by software.

The imported single-arm runner rejects a legacy S4 calibration object before paid preflight. With no valid bound S5 gate, it retains full observations. An explicitly mismatched receipt fails before capture rather than silently claiming projection permission. Paid-request authorization is separate and is never granted by a risk receipt. A retained old selector remains available for historical arithmetic reproduction, but its deployable flag is not accepted by the new entrypoint. No valid new deployment receipt is issued for the historical campaign.

### 8.3 Validation and release boundaries

The revision includes tests for unauthorized margin expansion, consistent local rewrites against an external digest, incomplete frames, unknown labels, source overlap, false selected-action labels, changed scorer/runtime identities, duplicate JSON, and denominator preservation. A numerical grid independently compares fixed and alpha-spending bounds with SciPy. The current source includes the complete agent and imported research adapter; raw private trajectories are distributed separately from the GitHub review branch.

Testing is not a new independent model review. No real model or new Docker/Harbor campaign is run for this revision. Fixed dependencies are restored for local Node/type checks rather than represented as a new clean install. The new guard is exercised by contract and preflight tests; fresh native/container testing of this changed deployment path remains separate. Detailed commands and results are in the release validation record rather than inferred from earlier versions.

## 9. Prospective research plan

The next study should freeze the question frame, source grouping, candidate, scoring contract, and resource endpoints before observing calibration outcomes. Questions should not be repeatedly enlarged until projection occurs. Natural no-eligible outcomes, capture stops, incorrect answers, and unresolved transport cases belong in the original denominator. Positive results from the current campaign remain development evidence, and the observed sources cannot be relabeled as untouched holdout data.

First, execute a small, fixed debugging batch on new development material solely to establish that the locked pipeline can measure its endpoints. This batch is not a noninferiority study. Then evaluate one frozen candidate on a genuinely source-separated frame with a predeclared stopping rule. Use one model fork and protocol at a time, preserving reasoning effort and tool rights. Include appropriate product and tool-matched full controls; the current restricted reader is not the full Pi agent. Counterbalance branch order before results and report cache categories even when backend cache state cannot be isolated.

For strategy learning, the immediate problem is not adding more complex handcrafted features. Training records must first represent adverse bounded outcomes rather than discarding unfinished branches. A future objective may prioritize completion at a fixed resource budget and only optimize complete resource use among admissible policies. Partial cost must never be interpreted as completed-task cost. Model-based search and evidence use should retain a no-intervention action. Changing a policy after this audit creates a new candidate requiring new calibration rather than a new interpretation of the old nine groups.

Finite evidence cannot prove absolute zero risk without further structural assumptions. Under a fixed independent zero-harm example, 59 units are needed merely for a one-sided 95% upper bound below 5%; this is a numerical illustration, not approval of a 5% degradation margin or a new spending plan. When the desired precision is unaffordable, the scientific response is a narrower descriptive claim, not a relaxed margin chosen to obtain deployment status.

## 10. Limitations and conclusion

This is a retrospective audit of an adaptively developed source-reading campaign. Source labels are not proof of statistical independence, questions are investigator-authored, scoring definitions differ across historical phases, and successful interface tests do not establish behavior under malicious inputs. A private provider's returned name does not authenticate the backend model. Reported token counts do not authenticate a bill. The read-only replay protocol is not a general mutable-environment snapshot, and neither a hash manifest nor a unit-test suite establishes real-world safety.

Within those limits, the result is concrete. A learned threshold was marked deployable from selected completed records, while an omitted, selected continuation failed to answer within its declared cap. A mismatch between the provided authorization and evaluated margin further prevents treating that artifact as permission. The software revision preserves evidence, refuses unsupported permission, and supplies a stricter versioned scorer and complete-frame assessment. It does not remove genuine positive pairs or claim that projection never helps. ReVerPi's next contribution must be a reproducible capability-and-resource gain under a valid prospective design, not an increasingly permissive route from exploratory success to a green deployment flag.

## References

[1] Liu et al. SoL-Pi: Recursively Scaling Auto-Research Loops for Efficient Agent Harness. arXiv:2609.20519, 2026. Official repository: https://github.com/NVlabs/SoL-Pi . Current README and fixed prior source consulted; no reproduction of its benchmark numbers is claimed.

[2] Lindenbauer et al. The Complexity Trap: Simple Observation Masking Is as Efficient as LLM Summarization for Agent Context Management. arXiv:2508.21433v3, 2025. https://arxiv.org/abs/2508.21433

[3] Kang et al. ACON: Optimizing Context Compression for Long-horizon LLM Agents. arXiv:2510.00615v3. https://arxiv.org/abs/2510.00615

[4] Min et al. Toward Reliable Context Compression for Long-Horizon Agents: An Empirical Study of Execution Instability. arXiv:2608.06503. https://arxiv.org/abs/2608.06503

[5] Weinberger and Hozez. Token Reduction Is Not Cost Reduction. arXiv:2607.12161. https://arxiv.org/abs/2607.12161

[6] Bates, Angelopoulos, Lei, Malik, and Jordan. Distribution-Free, Risk-Controlling Prediction Sets. arXiv:2101.02703v3, 2021. https://arxiv.org/abs/2101.02703

[7] Howard, Ramdas, McAuliffe, and Sekhon. Time-uniform, nonparametric, nonasymptotic confidence sequences. Annals of Statistics 49(2), 1055-1080, 2021. arXiv:1810.08240v9. https://arxiv.org/abs/1810.08240

[8] Sutton. The Bitter Lesson. 2019. Author essay; general-method motivation only.

[E1] User-supplied S4 complete delivery, 24 September 2026; immutable input archive identified in Section 5. Original files are not silently replaced by this revision.

[E2] S5 census.json, request_usage.csv, run_census.csv, original-auditor rerun index, numerical cross-check, and release validation record. Recompute from E1 with the included offline census; aggregate outputs do not contain raw prompts or proxy credentials.
