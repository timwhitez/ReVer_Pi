#!/usr/bin/env python3
"""S4c deployed-mode runner: capture once, then dispatch ONLY the arm the frozen rule selects.

The frozen paired runner always dispatches both arms. Deployment needs one arm. This
module reuses that runner's own ``phase_run`` (same gateway, projection, archive,
recording transport and central ledger) and replaces only the branch selection step:
after the capture phase stops at the first eligible boundary, the pre-decision feature
vector is recomputed from the sealed boundary and tape, the fitted candidate rule is
evaluated, and exactly one arm is dispatched.

S5 correction: a legacy `deployable=true` value is not permission. New runs default
to the full arm unless an independently bound S5 gate permits projection. Paid-request
authorization remains a separate preflight. This runner does not fit or retune a policy. The decision, its inputs and both artifact digests are written to
``DEPLOY_DECISION.json`` before the arm is dispatched.
"""
from __future__ import annotations
import argparse, asyncio, sys, hashlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for path in (REPO, REPO / "src", REPO / "scripts", REPO / "research/s4", Path(__file__).resolve().parent):
    sys.path.insert(0, str(path))

from reverpi.util import atomic_write, canonical, digest, source_manifest, strict_json_loads, unseal_cache  # noqa: E402
from reverpi.paired_prefix import require  # noqa: E402
from reverpi.ledger import Ledger  # noqa: E402
from reverpi_sources import analysis, contracts, runner  # noqa: E402
from reverpi_sources.contracts import (SourcePlan, load_task, new_output, read_file,  # noqa: E402
                                       render_prompt, tree_manifest)
from reverpi_sources.runner import phase_run, preflight  # noqa: E402
from reverpi_sources.selector import action  # noqa: E402

contracts.S4 = runner.S4 = Path(__file__).resolve().parent
from research.s5.assurance import validate_runtime_binding, checked_action

def assurance_runtime_sha256():
    inputs={"core":source_manifest(contracts.ROOT),"research":contracts.research_manifest(),
            "assurance":{p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in sorted((REPO/"research/s5").glob("*.py"))}}
    return digest(inputs)

def assurance_scorer_sha256():
    return hashlib.sha256((REPO/"research/s5/contracts.py").read_bytes()).hexdigest()



def predecision_features(run: Path) -> dict:
    boundary = unseal_cache((run / "boundary.json").read_text())
    tape = unseal_cache((run / "tape.json").read_text())["entries"]
    cached = analysis.cache_fraction(tape[-1]["response"].get("usage")) if tape else None
    return {"eligible_bytes": sum(x["original_bytes"] for x in boundary["prepared"]["observations"] if x["eligible"]),
            "history_bytes": len(canonical(boundary["source"]["messages"]).encode("utf-8")),
            "eligible_count": boundary["prepared"]["eligible_count"],
            "prefix_requests": len(tape),
            "last_cached_fraction": cached}


async def run_deployed(plan_path, plan_sha, out, *, allow_paid=False, acknowledge_local=False,
                       authorization=None, candidate_path=None, calibration_path=None,
                       gate_path=None, gate_sha256=None):
    value = strict_json_loads(Path(plan_path).read_bytes())
    require(digest(value) == plan_sha, "Independent plan hash mismatch")
    plan = SourcePlan.model_validate(value)
    task = load_task(contracts.S4 / "data/tasks" / f"{plan.task_id}.json")
    require(candidate_path is not None,"A frozen candidate is required")
    require(calibration_path is None,"Legacy S4 calibration is not a deployment authorization; use a bound S5 gate or default full")
    candidate = strict_json_loads(Path(candidate_path).read_bytes())
    gate = strict_json_loads(Path(gate_path).read_bytes()) if gate_path is not None else None
    model_fork = "flash" if plan.provider.model == "deepseek-flash" else ("luna" if plan.provider.model == "gpt-6-luna" else "mock")
    if plan.provider.mock: model_fork=candidate.get("model_fork")
    runtime_sha=assurance_runtime_sha256();scorer_sha=assurance_scorer_sha256()
    validate_runtime_binding(candidate,gate,expected_gate_sha256=gate_sha256,model_fork=model_fork,
                             runtime_sha256=runtime_sha,scorer_sha256=scorer_sha)
    preflight(plan, task, allow_paid=allow_paid, acknowledge_local=acknowledge_local, authorization=authorization)
    original = contracts.S4 / "data/sources" / task["source_id"]
    require(tree_manifest(original) == task["files"], "Installed source snapshot differs from task")
    out = new_output(Path(out)); out.mkdir(parents=True, mode=0o700)
    atomic_write(out / "PLAN.json", canonical(value))
    atomic_write(out / "TASK.json", canonical(task))
    atomic_write(out / "EXECUTOR.json", canonical({"core": source_manifest(contracts.ROOT),
                                                   "research": contracts.research_manifest()}))
    (out / "workspace").mkdir(mode=0o700)
    files = {}
    for name in task["files"]:
        data = read_file(original, name)
        atomic_write(out / "workspace" / name, data, mode=0o444)
        files[name] = data.decode("utf-8")
    identities = tree_manifest(out / "workspace")
    prompt = render_prompt(task)
    atomic_write(out / "PUBLIC.json", canonical({"task_sha256": task["task_sha256"],
                                                "snapshot_sha256": task["snapshot_sha256"],
                                                "prompt": prompt, "gold_in_actor_workspace": False}))
    summary = {"schema": 1, "release": "Research-S4", "status": "running", "plan_sha": plan_sha,
               "source_sha256": plan.source_sha256, "research_sha256": plan.research_sha256,
               "provider_mock": plan.provider.mock, "real_model_calls": 0 if plan.provider.mock else None,
               "deployed_mode": True, "paired_effect_identified": False,
               "quality_or_superiority_established": False, "scripted_actor_only": plan.provider.mock,
               "read_only_replay_not_os_snapshot": True,
               "natural_coverage_population": "inspected_development_source_not_population_sample",
               "phases": []}
    atomic_write(out / "summary.json", canonical(summary))
    try:
        capture = await phase_run(out, "capture", plan, files, prompt, task, identities)
        summary["phases"].append(capture)
        atomic_write(out / "summary.json", canonical(summary))
        if capture["state"] != "boundary_captured":
            summary["status"] = "no_eligible_prefix" if capture["state"] == "completed" else "stopped"
        else:
            features = predecision_features(out)
            arm = checked_action(candidate,features,gate,expected_gate_sha256=gate_sha256,
                                 model_fork=model_fork,runtime_sha256=runtime_sha,scorer_sha256=scorer_sha)
            require(arm in {"full", "projected"}, "Invalid deployment decision")
            decision = {"schema": "reverpi.s5.deploy-decision.v1", "task_id": task["task_id"],
                        "plan_sha256": plan_sha, "features": features, "chosen_arm": arm,
                        "other_arm_not_dispatched": True,
                        "candidate_sha256": candidate["candidate_sha256"],
                        "gate_sha256": gate_sha256, "rule": candidate["rule"],
                        "runtime_sha256":runtime_sha,"scorer_sha256":scorer_sha,
                        "permission":"conditional_gate" if gate and gate.get("allow_projection") else "default_full_no_permission",
                        "gate_scope":gate.get("scope") if gate else None}
            atomic_write(out / "DEPLOY_DECISION.json", canonical(decision))
            row = await phase_run(out, arm, plan, files, prompt, task, identities)
            summary["phases"].append(row)
            summary["status"] = "deployed_single_arm_complete" if row["state"] == "completed" else "stopped"
            if row["state"] != "completed":
                summary["deploy_stop_reason"] = row.get("reason")
    except (Exception, asyncio.CancelledError) as exc:
        # Deployed mode must never fail silently: the gate refusal or transport error
        # is the operator's only signal, so record its kind and message.
        summary.update(status="stopped", error_type=type(exc).__name__,
                       error_message=str(exc)[:400],
                       error_phase="capture" if not summary["phases"] else
                       ("decision" if len(summary["phases"]) == 1 else summary["phases"][-1]["phase"]))
        if isinstance(exc, asyncio.CancelledError):
            raise
    finally:
        summary["central_cost"] = Ledger(out / "accounting.sqlite", plan.budget).totals()
        summary["source_unchanged"] = (digest(source_manifest(contracts.ROOT)) == plan.source_sha256
                                       and digest(contracts.research_manifest()) == plan.research_sha256)
        summary["fixture_unchanged"] = tree_manifest(out / "workspace") == identities
        if not summary["source_unchanged"] or not summary["fixture_unchanged"]:
            summary["status"] = "stopped"
        summary["common_prefix_cost"] = next((r["central_cost"] for r in summary["phases"]
                                              if r["phase"] == "capture"), {})
        summary["accounting_note"] = ("Deployed mode dispatches the capture prefix once and the selected "
                                     "arm only; the unselected arm is never purchased.")
        atomic_write(out / "summary.json", canonical(summary))
    return summary


def audit_deployed(run: Path, *, candidate=None, gate=None, expected_gate_sha256=None) -> dict:
    """Minimal deployed-mode audit: boundary identity, single-arm dispatch, no second arm."""
    run = Path(run)
    summary = strict_json_loads((run / "summary.json").read_bytes())
    require(summary.get("deployed_mode") is True, "Not a deployed-mode run")
    if not (run / "DEPLOY_DECISION.json").is_file():
        # A deployment pipeline must be able to state the negative outcome explicitly:
        # capture ended before a boundary, or stopped, and no arm was ever dispatched.
        phases = [p["phase"] for p in summary["phases"]]
        require(phases == ["capture"], "No decision was recorded but an arm phase exists")
        capture = summary["phases"][0]
        require(summary["status"] in {"no_eligible_prefix", "stopped"}, "Undeployed run has a deployment status")
        require(not (run / "full").exists() and not (run / "projected").exists(),
                "An arm directory exists without a recorded decision")
        return {"schema": "reverpi.s4c.deployed-audit.v1", "kind": "reverpi.s4c.deployed-audit",
                "task_id": summary.get("task_id") or strict_json_loads((run / "TASK.json").read_bytes())["task_id"],
                "status": summary["status"], "deployed": False,
                "capture_state": capture["state"], "capture_reason": capture.get("reason"),
                "prefix_requests": capture.get("experiment_dispatches"),
                "unselected_arm_absent": True, "no_arm_dispatched": True,
                "actual_requests": summary["central_cost"]["attempts"],
                "unknown_attempts": summary["central_cost"]["unknown_attempts"],
                "error_type": summary.get("error_type"), "error_message": summary.get("error_message"),
                "status_ok": True}
    decision = strict_json_loads((run / "DEPLOY_DECISION.json").read_bytes())
    phases = {p["phase"]: p for p in summary["phases"]}
    require(list(phases) == ["capture", decision["chosen_arm"]], "Deployed run did not use exactly capture + one arm")
    other = "projected" if decision["chosen_arm"] == "full" else "full"
    require(not (run / other).exists(), "The unselected arm was dispatched")
    require(decision["other_arm_not_dispatched"] is True, "Decision did not record single-arm dispatch")
    tape = unseal_cache((run / "tape.json").read_text())["entries"]
    boundary = unseal_cache((run / "boundary.json").read_text())
    require(len(tape) == len(boundary["prefix_length"]) if isinstance(boundary["prefix_length"], list)
            else len(tape) == boundary["prefix_length"], "Prefix length changed after capture")
    features = predecision_features(run)
    require(canonical(features) == canonical(decision["features"]), "Decision features do not match the sealed boundary")
    arm_row = phases[decision["chosen_arm"]]
    require(arm_row["prefix_replays"] == len(tape), "The chosen arm did not replay the full prefix")
    require(arm_row["fixture_unchanged"] is True, "Workspace changed during the arm")
    expected_mode = "projected" if decision["chosen_arm"] == "projected" else "full"
    require(expected_mode == decision["chosen_arm"], "Phase/mode mismatch")
    # Independent re-evaluation: recompute the rule on the recorded features and require the
    # recorded arm to be the one the rule actually selects. The decision file carries the rule,
    # so this check needs no external artifact and cannot silently follow a retuned rule.
    if decision.get("schema")=="reverpi.s5.deploy-decision.v1":
        require(candidate is not None,"S5 audit needs independently supplied candidate")
        require(decision["gate_sha256"]==expected_gate_sha256,"Decision gate differs from external gate")
        require(decision["rule"]==candidate["rule"] and decision["candidate_sha256"]==candidate["candidate_sha256"],"Decision rule changed")
        recomputed=checked_action(candidate,features,gate,expected_gate_sha256=expected_gate_sha256,
                                  model_fork=candidate["model_fork"],runtime_sha256=decision["runtime_sha256"],scorer_sha256=decision["scorer_sha256"])
    else:
        # Retained only to inspect historical arithmetic; NOT permission verification.
        recomputed="projected" if action(decision["rule"],features) else "full"
    require(recomputed == decision["chosen_arm"], "Recorded arm is not the bound rule/permission decision")
    return {"schema": "reverpi.s4c.deployed-audit.v1", "kind": "reverpi.s4c.deployed-audit",
            "deployed": True,
            "rule_reevaluation": recomputed,
            "task_id": decision["task_id"], "status": summary["status"], "chosen_arm": decision["chosen_arm"],
            "unselected_arm_absent": True, "prefix_requests": len(tape),
            "decided_from": features, "arithmetic": "capture + 1 arm only",
            "planned_requests": arm_row["experiment_dispatches"] + len(tape),
            "actual_requests": summary["central_cost"]["attempts"],
            "unknown_attempts": summary["central_cost"]["unknown_attempts"],
            "candidate_sha256": decision["candidate_sha256"],
            "calibration_sha256": decision.get("calibration_sha256"), "gate_sha256":decision.get("gate_sha256"),
            "permission_binding_checked":decision.get("schema")=="reverpi.s5.deploy-decision.v1", "status_ok": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("run")
    a.add_argument("--plan", type=Path, required=True)
    a.add_argument("--plan-sha256", required=True)
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--candidate", type=Path, required=True)
    a.add_argument("--calibration", type=Path, help="Rejected for new runs; historical gate is not permission")
    a.add_argument("--gate", type=Path)
    a.add_argument("--gate-sha256")
    a.add_argument("--allow-paid", action="store_true")
    a.add_argument("--acknowledge-local-readonly", action="store_true")
    a.add_argument("--authorization", type=Path)
    b = sub.add_parser("audit")
    b.add_argument("--run", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--candidate",type=Path)
    b.add_argument("--gate",type=Path)
    b.add_argument("--gate-sha256")
    args = parser.parse_args()
    if args.cmd == "run":
        result = asyncio.run(run_deployed(args.plan, args.plan_sha256, args.out,
                                          allow_paid=args.allow_paid,
                                          acknowledge_local=args.acknowledge_local_readonly,
                                          authorization=args.authorization,
                                          candidate_path=args.candidate, calibration_path=args.calibration,
                                          gate_path=args.gate,gate_sha256=args.gate_sha256))
        print(canonical({"status": result["status"], "mock": result["provider_mock"],
                         "central_cost": result["central_cost"]}))
        return 0 if result["status"] in {"deployed_single_arm_complete", "no_eligible_prefix"} else 2
    out = new_output(args.out)
    candidate=strict_json_loads(args.candidate.read_bytes()) if args.candidate else None
    gate=strict_json_loads(args.gate.read_bytes()) if args.gate else None
    result=audit_deployed(args.run,candidate=candidate,gate=gate,expected_gate_sha256=args.gate_sha256)
    atomic_write(out,canonical(result))
    print(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
