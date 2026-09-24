#!/usr/bin/env python3
"""S5-B development re-analysis on the observed S4 campaign (offline, zero cost).

Reads the host census (derived byte-identically from the delivered S4 archive) and
answers three questions the completed-pair-only calibration could not:

  1. What does the frozen rule (`history_bytes >= 93641 -> projected`) actually do
     across *all* submitted eligible runs, including the ones that stopped?
  2. How much of that is censored (arm never dispatched / not completed) rather than
     a measured success or failure?
  3. If a decision rule is fitted on these observations with bounded failure counted
     as failure, does the frozen threshold survive -- and does it transfer when a
     whole source group is held out?

Nothing here is a validation on fresh sources. The observed runs were selected by the
development process, six runs are single-arm by design, and branch order came from a
family-parity rule, not randomisation. Every number below is development evidence.
"""
from __future__ import annotations
import json, collections, argparse
from pathlib import Path

ARMS = ("full", "projected")
RULES = ("extracted", "strict_v2")
SCORE_KEYS = {"extracted": "legacy_extracted_descriptive", "strict_v2": "strict_v2_descriptive"}
RULE_THRESHOLD = 93641.0
RULE_FEATURE = "history_bytes"


def arm(record: dict, name: str) -> dict | None:
    for phase in record["phases"]:
        if phase["phase"] == name:
            return phase
    return None


def outcome(phase: dict | None, contract: str) -> str:
    """correct | wrong | capped | absent | other:<reason> -- capped is failure at the frozen cap."""
    if phase is None:
        return "absent"
    if phase["state"] == "completed":
        score = phase.get(SCORE_KEYS[contract])
        if score is None:
            return "absent"
        return "correct" if score["correct"] else "wrong"
    if phase.get("reason") == "suffix_cap":
        return "capped"
    return f"other:{phase.get('reason')}"


def succeeded(value: str) -> bool:
    return value == "correct"


def failed_at_cap(value: str) -> bool:
    # wrong is a scored wrong answer; capped is the frozen-budget failure endpoint.
    return value in {"wrong", "capped"}


def build_rows(census: dict) -> list[dict]:
    rows = []
    for record in census["runs"]:
        if "features" not in record:
            continue  # no sealed boundary: the rule has no decision point to act on
        chosen = "projected" if record["features"][RULE_FEATURE] >= RULE_THRESHOLD else "full"
        row = {"run_id": record["run_id"], "bucket": record["bucket"], "task_id": record["task_id"],
               "source_group": record["source_group"], "summary_state": record["summary_state"],
               "pair_complete": record["pair_complete"], "history_bytes": record["features"][RULE_FEATURE],
               "rule_choice": chosen, "arms": {}}
        for contract in RULES:
            values = {name: outcome(arm(record, name), contract) for name in ARMS}
            row["arms"][contract] = values
        for name in ARMS:
            phase = arm(record, name)
            row[f"{name}_tokens"] = phase["observed_tokens"] if phase else 0
            row[f"{name}_requests"] = phase["requests"] if phase else 0
        rows.append(row)
    return rows


def run_row(row: dict, contract: str) -> dict:
    values = row["arms"][contract]
    chosen, other = row["rule_choice"], ("projected" if row["rule_choice"] == "full" else "full")
    chosen_value, other_value = values[chosen], values[other]
    if succeeded(chosen_value):
        verdict = "success"
    elif failed_at_cap(chosen_value) and succeeded(other_value):
        verdict = "confirmed_harm"
    elif failed_at_cap(chosen_value) and failed_at_cap(other_value):
        verdict = "both_failed"
    elif failed_at_cap(chosen_value) and other_value == "absent":
        verdict = "unresolved_failure"
    elif chosen_value == "absent":
        verdict = "unobserved"
    else:
        verdict = f"other:{chosen_value}"
    return {"task_id": row["task_id"], "source_group": row["source_group"], "history_bytes": row["history_bytes"],
            "chosen_arm": chosen, "chosen_outcome": chosen_value, "other_outcome": other_value,
            "chosen_tokens": row[f"{chosen}_tokens"], "verdict": verdict, "bucket": row["bucket"],
            "summary_state": row["summary_state"]}


def summarise(rows: list[dict], contract: str) -> dict:
    per_run = [run_row(row, contract) for row in rows]
    counts = collections.Counter(r["verdict"] for r in per_run)
    by_arm = collections.Counter(r["chosen_arm"] for r in per_run)
    harm_groups = sorted({r["source_group"] for r in per_run if r["verdict"] == "confirmed_harm"})
    unresolved_groups = sorted({r["source_group"] for r in per_run if r["verdict"] == "unresolved_failure"})
    selection = [r for r in per_run if r["chosen_arm"] == "projected"]
    return {"contract": contract, "runs": len(per_run), "chosen_full": by_arm["full"],
            "chosen_projected": by_arm["projected"], "verdicts": dict(sorted(counts.items())),
            "confirmed_harm_runs": [r["task_id"] for r in per_run if r["verdict"] == "confirmed_harm"],
            "confirmed_harm_groups": harm_groups, "unresolved_groups": unresolved_groups,
            "tokens_spent_on_rule_choice": sum(r["chosen_tokens"] for r in per_run),
            "tokens_spent_projecting": sum(r["chosen_tokens"] for r in selection),
            "rows": per_run}


def always(rows: list[dict], arm_name: str, contract: str) -> dict:
    """Counterfactual of an always-X rule, using only runs where X was actually observed."""
    values, observed, correct, capped, unresolved = [], 0, 0, 0, 0
    for row in rows:
        value = row["arms"][contract][arm_name]
        if value == "absent":
            unresolved += 1
            continue
        observed += 1
        values.append(row[f"{arm_name}_tokens"])
        if succeeded(value):
            correct += 1
        elif failed_at_cap(value):
            capped += 1
    return {"rule": f"always_{arm_name}", "contract": contract, "observed_runs": observed,
            "correct": correct, "failed_at_cap": capped, "unobserved_runs": unresolved,
            "tokens_in_observed_runs": sum(values)}


def threshold_curve(rows: list[dict], contract: str) -> list[dict]:
    """Every distinct history_bytes threshold: what the rule would select and what happened."""
    cutpoints = sorted({row["history_bytes"] for row in rows})
    curve = []
    for cut in cutpoints:
        selected = [row for row in rows if row["history_bytes"] >= cut]
        correct = sum(1 for row in selected if succeeded(row["arms"][contract]["projected"]))
        capped = sum(1 for row in selected if failed_at_cap(row["arms"][contract]["projected"]))
        unobserved = sum(1 for row in selected if row["arms"][contract]["projected"] == "absent")
        harnessed = sum(1 for row in selected
                        if failed_at_cap(row["arms"][contract]["projected"])
                        and succeeded(row["arms"][contract]["full"]))
        curve.append({"threshold": cut, "selected": len(selected), "projected_correct": correct,
                      "projected_failed_at_cap": capped, "projected_unobserved": unobserved,
                      "confirmed_selected_harm": harnessed,
                      "projected_tokens_on_selected": sum(row["projected_tokens"] for row in selected)})
    return curve


def paired_rows(rows: list[dict], contract: str) -> list[dict]:
    """Runs where both arms were actually dispatched, so outcomes and cost are comparable."""
    return [row for row in rows
            if row["arms"][contract]["full"] != "absent" and row["arms"][contract]["projected"] != "absent"]


def rule_cost(rows: list[dict], contract: str, cut: float) -> dict:
    """Cost and failure count of `history_bytes >= cut -> projected` over comparable runs.

    A run counts as failed when the rule-chosen arm produced no correct final answer within
    the frozen budget (wrong answer or cap exhausted). Cost is the rule-chosen arm's tokens,
    so an always-full rule costs what full costs; this is what makes "select nothing" a real
    baseline instead of a free option.
    """
    selected = [row for row in rows if row["history_bytes"] >= cut]
    failures, cost = 0, 0
    for row in rows:
        chosen = "projected" if row["history_bytes"] >= cut else "full"
        if not succeeded(row["arms"][contract][chosen]):
            failures += 1
        cost += row[f"{chosen}_tokens"]
    projected_cost = sum(row["projected_tokens"] for row in selected)
    return {"threshold": cut, "selected_runs": len(selected), "failures": failures,
            "cost_tokens": cost, "projected_arm_tokens_on_selected": projected_cost,
            "all_full_cost_tokens": sum(row["full_tokens"] for row in rows)}


def threshold_surface(rows: list[dict], contract: str) -> list[dict]:
    cuts = sorted({row["history_bytes"] for row in rows} | {float("inf")})
    return [rule_cost(rows, contract, cut) for cut in cuts]


def baseline(rows: list[dict], contract: str, arm_name: str) -> dict:
    """Always-X baseline restricted to runs where X was observed (used on comparable runs)."""
    failures = sum(1 for row in rows if not succeeded(row["arms"][contract][arm_name]))
    return {"arm": arm_name, "runs": len(rows), "failures": failures,
            "correct": len(rows) - failures, "cost_tokens": sum(row[f"{arm_name}_tokens"] for row in rows)}


def leave_one_source_out(rows: list[dict], contract: str) -> dict:
    """Fit the threshold on all but one source group, evaluate on the held-out group.

    Development objective, lexicographic: fewest failures (no correct final answer from the
    rule-chosen arm), then lowest token cost of the rule-chosen arms, then the lowest
    threshold (deterministic; ties keep more runs on the full arm).
    """
    groups = sorted({row["source_group"] for row in rows})
    folds = []
    for held in groups:
        train = [row for row in rows if row["source_group"] != held]
        test = [row for row in rows if row["source_group"] == held]
        if not train or not test:
            continue
        candidates = sorted(((entry["failures"], entry["cost_tokens"], entry["threshold"])
                             for entry in threshold_surface(train, contract)))
        cut = candidates[0][2]
        fitted = rule_cost(test, contract, cut)
        frozen = rule_cost(test, contract, RULE_THRESHOLD)
        folds.append({"held_out_group": held, "test_runs": len(test), "fitted_threshold": cut,
                      "fitted_selected": fitted["selected_runs"], "fitted_failures": fitted["failures"],
                      "fitted_cost_tokens": fitted["cost_tokens"],
                      "frozen_selected": frozen["selected_runs"], "frozen_failures": frozen["failures"],
                      "frozen_cost_tokens": frozen["cost_tokens"]})
    finite = [fold["fitted_threshold"] for fold in folds if fold["fitted_threshold"] != float("inf")]
    return {"folds": folds, "folds_held_out": len(folds),
            "fitted_thresholds": sorted({fold["fitted_threshold"] for fold in folds}),
            "folds_reproducing_frozen_threshold": sum(1 for fold in folds
                                                      if fold["fitted_threshold"] == RULE_THRESHOLD),
            "folds_choosing_no_projection": sum(1 for fold in folds if fold["fitted_threshold"] == float("inf")),
            "fitted_total_failures": sum(fold["fitted_failures"] for fold in folds),
            "frozen_total_failures": sum(fold["frozen_failures"] for fold in folds),
            "fitted_total_selected": sum(fold["fitted_selected"] for fold in folds),
            "frozen_total_selected": sum(fold["frozen_selected"] for fold in folds),
            "finite_thresholds_seen": finite}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--selftest", action="store_true",
                        help="assert the headline numbers this analysis is published with")
    args = parser.parse_args()
    census = json.loads(args.census.read_text())
    rows = build_rows(census)
    comparable = {contract: paired_rows(rows, contract) for contract in RULES}
    report = {
        "schema": "reverpi.s5b.dev-reanalysis.v1",
        "census_sha256_input": census.get("totals", {}),
        "scope": ("development re-analysis of all submitted runs that reached a sealed boundary; "
                  "not a sample of a natural source population and not a validation frame"),
        "frozen_rule": {"feature": RULE_FEATURE, "threshold": RULE_THRESHOLD, "direction": "ge",
                        "action": "projected when true, else full"},
        "boundary_runs": len(rows),
        "source_groups": sorted({row["source_group"] for row in rows}),
        "single_arm_runs": sum(1 for row in rows if row["bucket"] == "cumulative_runs/deployed"),
        "summaries": {contract: {k: v for k, v in summarise(rows, contract).items() if k != "rows"}
                      for contract in RULES},
        "rule_rows": {contract: summarise(rows, contract)["rows"] for contract in RULES},
        "counterfactuals": {contract: [always(rows, name, contract) for name in ARMS] for contract in RULES},
        "threshold_curves": {contract: threshold_curve(rows, contract) for contract in RULES},
        "comparable_both_arms": {contract: {
            "runs": len(comparable[contract]),
            "baselines": [baseline(comparable[contract], contract, name) for name in ARMS],
            "frozen_rule": rule_cost(comparable[contract], contract, RULE_THRESHOLD),
            "surface": threshold_surface(comparable[contract], contract),
            "leave_one_source_out": leave_one_source_out(comparable[contract], contract),
        } for contract in RULES},
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "S5B_DEV_REANALYSIS.json").write_text(json.dumps(report, indent=1, sort_keys=True))
    if args.selftest:
        primary = report["summaries"]["extracted"]
        comparable = report["comparable_both_arms"]["extracted"]
        assert report["boundary_runs"] == 33, report["boundary_runs"]
        assert report["single_arm_runs"] == 6, report["single_arm_runs"]
        assert primary["chosen_full"] == 22 and primary["chosen_projected"] == 11, primary
        assert primary["confirmed_harm_runs"] == ["pathspec-util"], primary["confirmed_harm_runs"]
        assert primary["verdicts"]["success"] == 19, primary["verdicts"]
        assert primary["verdicts"]["unresolved_failure"] == 7 and primary["verdicts"]["unobserved"] == 4
        assert comparable["runs"] == 17, comparable["runs"]
        baselines = {row["arm"]: row for row in comparable["baselines"]}
        assert baselines["full"]["failures"] == 3 and baselines["projected"]["failures"] == 5, baselines
        assert comparable["frozen_rule"]["failures"] == 3, comparable["frozen_rule"]
        assert comparable["frozen_rule"]["cost_tokens"] > baselines["full"]["cost_tokens"], "frozen rule state"
        loo = comparable["leave_one_source_out"]
        assert loo["folds_held_out"] == 14 and loo["folds_reproducing_frozen_threshold"] == 1, loo
        print("selftest ok: 33 boundary runs, 11 projected, 1 confirmed harm, 17 comparable, "
              "always-full 3 failures, frozen rule no cheaper, LOO reproduces frozen threshold 1/14")
    primary = report["summaries"]["extracted"]
    block = report["comparable_both_arms"]["extracted"]
    print(json.dumps({"boundary_runs": report["boundary_runs"], "single_arm_runs": report["single_arm_runs"],
                      "extracted": {k: v for k, v in primary.items() if k != "contract"},
                      "comparable_runs": block["runs"], "frozen_rule_on_comparable": block["frozen_rule"],
                      "loo": {k: v for k, v in block["leave_one_source_out"].items() if k != "folds"}}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
