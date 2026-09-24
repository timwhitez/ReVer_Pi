"""Flash-only orchestration: two small development slots and one scoped review.

A bounded workflow around existing execution contracts, not a new agent or a
replacement formal evaluation gate. Each slot has ONE immutable run directory.
Token allocations are admission budgets, not physical caps on an uncooperative
provider. Unspent allocations are never silently transferred to another slot.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

from .config import Budget, CompressionConfig, Provider
from .errors import LabError
from .intervention_matrix import prepare_matrix, execute_matrix
from .lean_review import require_flash, prepare_review, run_review
from .research_audit import audit_ledger
from .util import atomic_write, canonical, digest, bytes_digest, source_manifest, strict_json_loads, process_lock, contained_regular_file


def init_campaign(root: Path, out: Path, provider: Provider, *, dev_tokens: int = 90000,
                  review_tokens: int = 120000, max_review_jobs: int = 8,
                  paid_execution_authorized: bool = False) -> dict:
    """Freeze a separate profile; no original provider/configuration is overwritten."""
    if type(paid_execution_authorized) is not bool:
        raise ValueError("Paid campaign authorization must be explicit")
    provider = provider.model_copy(update={"concurrency": 1})
    require_flash(provider)
    if any(type(x) is not int or x < 1 for x in (dev_tokens, review_tokens)):
        raise ValueError("Positive integer token allocations are required")
    if type(max_review_jobs) is not int or not 1 <= max_review_jobs <= 16:
        raise ValueError("Require 1..16 scoped review jobs")
    if not provider.mock and min(dev_tokens, review_tokens) <= provider.max_output_tokens + 4096:
        raise ValueError("A slot cannot admit even one conservative request; do not lower output reservation to fit")
    allocations = {"dev-01": dev_tokens, "dev-02": dev_tokens, "review": review_tokens}
    budgets = {name: Budget(max_total_tokens=value, max_attempts=80,
                           per_cell_attempts=16, per_cell_tokens=value).model_dump()
               for name, value in allocations.items()}
    plan = {"schema": 1, "kind": "flash_lean_campaign", "source_files": source_manifest(root),
            "provider": provider.model_dump(), "budgets": budgets,
            "total_allocated_tokens": sum(allocations.values()), "max_review_jobs": max_review_jobs,
            "paid_execution_authorized": paid_execution_authorized,
            "preflight_policy": "rc5_1_gold_env_output_stress",
            "diagnostic_paid_calls_authorized": False,
            "dev_repeats": 1, "max_turns": 6, "seed": 20260919,
            "interrupted_policy": "halt", "formal_gate_passed": False,
            "physical_billing_cap_proven": False,
            "pricing": "token_only_unless_frozen_user_prices_configured",
            "external_benchmark_authorized": False,
            "note": "Two new development sources; eight availability cells, not population evidence. "
                    "One explicit scoped review. No full Terminal-Bench, full-tree review, or model fallback."}
    out.mkdir(parents=True, exist_ok=False)
    atomic_write(out / "campaign.json", canonical(plan))
    atomic_write(out / "provider.json", canonical(provider.model_dump()))
    for name, budget in budgets.items():
        atomic_write(out / ("budget-" + name + ".json"), canonical(budget))
    return {"campaign_sha256": digest(plan), "allocated_tokens": plan["total_allocated_tokens"],
            "provider": provider.model, "effort": provider.effort, "paid_calls": 0,
            "physical_billing_cap_proven": False, "external_benchmark_authorized": False}


def load_campaign(root: Path, out: Path, expected: str, *, check_source: bool = True) -> dict:
    if out.is_symlink():
        raise ValueError("Campaign path cannot be a symlink")
    plan = strict_json_loads(contained_regular_file(out, 'campaign.json').read_bytes())
    if plan.get("schema") != 1 or plan.get("kind") != "flash_lean_campaign" or digest(plan) != expected:
        raise ValueError("Campaign identity changed")
    require_flash(Provider.model_validate(plan["provider"]))
    if check_source and source_manifest(root) != plan["source_files"]:
        raise LabError("source_changed", "Do not modify a frozen campaign; use a separately authorized generation")
    return plan


def _dev_slot(plan: dict, slot: str) -> None:
    if slot not in {"dev-01", "dev-02"} or slot not in plan["budgets"]:
        raise ValueError("Only the two predeclared development slots are permitted")


def prepare_dev(root: Path, out: Path, campaign_sha: str, slot: str, *, parent: Path,
                parent_sha: str, workspace: Path, registry: Path, registry_sha: str,
                gold_sha: str, source_note: str, gold_certificate: Path | None = None) -> dict:
    plan = load_campaign(root, out, campaign_sha)
    _dev_slot(plan, slot)
    if not isinstance(source_note, str) or not source_note.strip() or len(source_note) > 2000:
        raise ValueError("Record a bounded provenance/independence note without answers")
    from .paired_interventions import read_intervention
    parent_obj = read_intervention(parent, parent_sha)
    certificate = None
    if gold_certificate is not None:
        from .preflight import validate_gold_certificate
        certificate = strict_json_loads(gold_certificate.read_bytes())
        validate_gold_certificate(certificate, parent_obj, parent_sha, gold_sha)
    if not plan["provider"]["mock"] and certificate is None:
        raise LabError("evaluation_preflight", "Live development requires controller-side gold schema certification")
    if parent_obj["memory"]["method"] != "mask":
        raise ValueError("The lean mechanism pilot freezes mask; no 13-method sweep")
    # Do not count a second trial on the same source as a second source.
    other = "dev-02" if slot == "dev-01" else "dev-01"
    with process_lock(out / "campaign.lock"):
        other_plan = out / other / "plan.json"
        if (out / other / "campaign_binding.json").exists() and not other_plan.exists():
            raise LabError("identity_changed", "Other prepared slot is missing its plan")
        if other_plan.exists():
            old = strict_json_loads(other_plan.read_bytes())
            if old["parent_sha256"] == parent_sha or old["source_group"] == parent_obj["checkpoint"]["source_group"]:
                raise ValueError("These development slots require distinct parent/source identifiers")
        result = prepare_matrix(out / slot, parent, parent_sha, Provider.model_validate(plan["provider"]),
                                Budget.model_validate(plan["budgets"][slot]),
                                CompressionConfig(recovery_search_mode="match"), workspace=workspace,
                                registry=registry, registry_sha=registry_sha, repeats=1, seed=plan["seed"],
                                max_turns=plan["max_turns"], recovery_calls=3, revalidation_calls=2,
                                interrupted_policy="halt", evaluation_gold_sha256=gold_sha)
        atomic_write(out / slot / "campaign_binding.json", canonical({"campaign_sha256": campaign_sha,
            "slot": slot, "plan_sha256": result["plan_sha256"], "source_note": source_note,
            "independent_source_verified": False,
            "gold_schema_certificate": certificate}))
    result["slot_binding_sha256"] = digest(strict_json_loads((out / slot / "campaign_binding.json").read_bytes()))
    return result


def prepare_campaign_review(root: Path, out: Path, campaign_sha: str, scope: list[dict]) -> dict:
    plan = load_campaign(root, out, campaign_sha)
    with process_lock(out / "campaign.lock"):
        result = prepare_review(root, out / "review", Provider.model_validate(plan["provider"]),
                                Budget.model_validate(plan["budgets"]["review"]), scope,
                                max_jobs=plan["max_review_jobs"])
        atomic_write(out / "review" / "campaign_binding.json", canonical({
            "campaign_sha256": campaign_sha, "slot": "review", "plan_sha256": result["plan_sha256"]}))
    result["slot_binding_sha256"] = digest(strict_json_loads((out / "review" / "campaign_binding.json").read_bytes()))
    return result


def validate_slot(out: Path, plan: dict, campaign_sha: str, slot: str, *,
                  expected_binding_sha: str | None = None) -> str:
    if slot not in plan["budgets"] or (out / slot).is_symlink():
        raise ValueError("Unregistered or symlinked campaign slot")
    binding = strict_json_loads(contained_regular_file(out, slot + '/campaign_binding.json').read_bytes())
    if expected_binding_sha is not None and digest(binding) != expected_binding_sha:
        raise ValueError("Slot binding differs from operator-held identity")
    child = strict_json_loads(contained_regular_file(out, slot + '/plan.json').read_bytes())
    if binding.get("campaign_sha256") != campaign_sha or binding.get("slot") != slot or digest(child) != binding.get("plan_sha256"):
        raise ValueError("Slot plan/campaign binding changed")
    if child["provider"] != plan["provider"] or child["budget"] != plan["budgets"][slot]:
        raise ValueError("Slot provider or token allocation changed")
    return binding["plan_sha256"]


async def run_slot(root: Path, out: Path, campaign_sha: str, slot: str, *, allow_paid: bool = False,
                   acknowledge_unsandboxed: bool = False, transport=None,
                   expected_binding_sha: str | None = None) -> dict:
    plan = load_campaign(root, out, campaign_sha)
    with process_lock(out / "campaign.lock"):
        sha = validate_slot(out, plan, campaign_sha, slot, expected_binding_sha=expected_binding_sha)
        preflight = campaign_preflight(root, out, campaign_sha, slot, expected_binding_sha=expected_binding_sha)
        if not plan["provider"]["mock"] and not preflight["ready_for_paid_dispatch"]:
            raise LabError("campaign_preflight", "Paid dispatch blocked: " + ",".join(preflight["blockers"]))
        # Any observed reservation breach in ANY slot stops the whole campaign.
        before = campaign_status(root, out, campaign_sha)
        if before["admission_breached"]:
            raise LabError("reservation_breach", "Another campaign slot exceeded its reservation/allocation")
        if slot == "review":
            result = await run_review(root, out / slot, sha, allow_paid=allow_paid, transport=transport)
        else:
            result = await execute_matrix(out / slot, sha, allow_paid=allow_paid,
                                          acknowledge_unsandboxed=acknowledge_unsandboxed, transport=transport)
        # Do not interpret task or review failures as permission to add a new slot.
        campaign_status(root, out, campaign_sha, write=True)
        return result


def campaign_preflight(root: Path, out: Path, campaign_sha: str, slot: str, *,
                       expected_binding_sha: str | None = None) -> dict:
    from .preflight import budget_screen, validate_gold_certificate, validate_provider_environment
    from .paired_interventions import read_intervention
    plan = load_campaign(root, out, campaign_sha)
    validate_slot(out, plan, campaign_sha, slot, expected_binding_sha=expected_binding_sha)
    child = strict_json_loads(contained_regular_file(out, slot + '/plan.json').read_bytes())
    provider = Provider.model_validate(plan["provider"])
    blockers = []
    if not provider.mock and expected_binding_sha is None:
        blockers.append("missing_operator_held_slot_binding_sha256")
    try:
        environment = validate_provider_environment(provider)
    except LabError as exc:
        environment = {"error": exc.kind, "message": exc.message, "paid_calls": 0}
        blockers.append("local_configuration")
    screen = budget_screen(out / slot, child)
    if not screen["stress_screen_passed"]:
        blockers.append("declared_budget_stress_not_funded")
    if not plan.get("paid_execution_authorized", False):
        blockers.append("no_new_paid_campaign_authorization")
    if plan.get("preflight_policy") != "rc5_1_gold_env_output_stress":
        blockers.append("legacy_campaign_read_only")
    if slot != "review":
        binding = strict_json_loads(contained_regular_file(out, slot + '/campaign_binding.json').read_bytes())
        parent = read_intervention(out / slot / "parent.json", child["parent_sha256"])
        try:
            validate_gold_certificate(binding.get("gold_schema_certificate"), parent,
                                      child["parent_sha256"], child["evaluation"]["gold_sha256"])
        except LabError:
            blockers.append("missing_or_invalid_gold_schema_certificate")
    return {"schema": 1, "slot": slot, "paid_calls": 0, "network_requests": 0,
            "environment": environment, "budget": screen, "blockers": blockers,
            "ready_for_paid_dispatch": not blockers, "formal_gate_passed": False,
            "external_slot_identity_checked": expected_binding_sha is not None,
            "identity_threat_model": "operator holds digest outside mutable campaign; not a digital signature",
            "remote_authentication_verified": False, "completion_funding_certified": False}


def campaign_status(root: Path, out: Path, campaign_sha: str, *, write: bool = False) -> dict:
    plan = load_campaign(root, out, campaign_sha, check_source=False)
    rows = []
    breached = False
    observed = unknown = 0
    for name, b in plan["budgets"].items():
        folder = out / name
        if folder.is_symlink():
            raise ValueError("A campaign slot is a symlink")
        row: dict[str, Any] = {"slot": name, "allocated_tokens": b["max_total_tokens"],
                               "prepared": (folder / "campaign_binding.json").exists()}
        if row["prepared"]:
            validate_slot(out, plan, campaign_sha, name)
        db = folder / "ledger.sqlite"
        if not db.exists() and ((folder / "arms").exists() or (folder / "report.json").exists() or (folder / "progress.json").exists()):
            raise ValueError("Execution evidence exists but its ledger is missing; do not reset spending")
        if db.is_symlink():
            raise ValueError("A campaign ledger is a symlink")
        if db.exists() and not row["prepared"]:
            raise ValueError("Unbound ledger found in campaign slot")
        if db.exists():
            audit = audit_ledger(db)
            totals = audit["totals"]
            row["usage"] = totals
            observed += totals["observed_tokens"]
            unknown += totals["unknown_reserved_tokens"]
            breached |= totals["accounted_tokens"] > b["max_total_tokens"] or totals["observed_over_reservation_attempts"] > 0
        rows.append(row)
    report = {"schema": 1, "campaign_sha256": campaign_sha, "slots": rows,
              "allocated_tokens": plan["total_allocated_tokens"], "observed_tokens": observed,
              "unknown_reserved_tokens": unknown, "accounted_tokens": observed + unknown,
              "admission_breached": bool(breached), "physical_billing_cap_proven": False,
              "source_unchanged": source_manifest(root) == plan["source_files"],
              "formal_gate_passed": False, "external_benchmark_authorized": False}
    if write:
        atomic_write(out / "status.json", canonical(report))
    return report


def audit_gen10(root: Path) -> dict:
    """Recompute from exported rows; never claim absent parent/SQLite were checked."""
    base = root / "reports/rc4_gen10_20260919"
    rows = []
    for folder_name, eval_name in (("arms_live", "realdev-eval-live.json"),
                                   ("arms2_live", "realdev2-eval.json")):
        evaluation = strict_json_loads((base / eval_name).read_bytes())
        plans = []
        cost = attempts = 0
        cells = []
        for file in sorted((base / folder_name).glob("*/result.json")):
            obj = strict_json_loads(file.read_bytes())
            unit = file.parent
            commit = strict_json_loads((unit / "COMMIT.json").read_bytes())
            if any(not isinstance(name, str) or name in {".", ".."} or "\\" in name or ":" in name
                   or Path(name).name != name or (unit / name).is_symlink() for name in commit["files"]):
                raise ValueError("Unsafe path in committed evidence")
            checks = {name: bytes_digest((unit / name).read_bytes()) == sha if sha else not (unit / name).exists()
                      for name, sha in commit["files"].items()}
            if not all(checks.values()):
                raise ValueError("A gen10 committed result changed")
            if commit["matrix_sha256"] != evaluation["plan_sha256"]:
                raise ValueError("Gen10 result/evaluation plan commitment mismatch")
            cost += obj["cost"]["known_tokens"]
            attempts += obj["cost"]["attempts"]
            plans.append(obj["parent_sha256"])
            cells.append({"unit": unit.name, "answers": obj.get("answers"),
                          "observed_tokens_from_result": obj["cost"]["known_tokens"],
                          "attempts_from_result": obj["cost"]["attempts"],
                          "recovery_calls": obj["recovery_calls"], "revalidation_calls": obj["revalidation_calls"]})
        rows.append({"source_group": evaluation["source_group"], "cells": cells,
                     "sum_result_tokens": cost, "sum_result_attempts": attempts,
                     "evaluation_report_tokens": evaluation["reader_cost"]["observed_tokens"],
                     "evaluation_report_attempts": evaluation["reader_cost"]["attempts"],
                     "cost_difference": evaluation["reader_cost"]["observed_tokens"] - cost,
                     "same_parent_commitment": len(set(plans)) == 1,
                     "parent_bytes_in_gen10_report_directory": (base / folder_name / "parent.json").is_file(),
                     "raw_gen10_ledger_in_gen10_report_directory": (base / folder_name / "ledger.sqlite").is_file()})
    builder = (base / "realdev2_checkpoint_builder.py").read_text(encoding="utf8")
    paired = (root / "src/reverpi/paired_interventions.py").read_text(encoding="utf8")
    return {"schema": 1, "sources": rows,
            "result_rows_tokens": sum(r["sum_result_tokens"] for r in rows),
            "evaluation_reports_tokens": sum(r["evaluation_report_tokens"] for r in rows),
            "explicit_rerun_obligation_in_builder": "Re-run the public tests before claiming current success." in builder,
            "historical_not_current_instruction_in_reader": "Archived evidence is historical, not current verification." in paired,
            "interpretation": "Observed tool-availability pattern under explicit prompts and self-authored fixtures; "
                              "not unprompted discovery, not two independent public benchmarks, not evidence of superiority to Pi.",
            "ledger_independently_recomputed": False, "formal_gate_passed": False,
            "instruction_flags_are_source_text_presence_only": True,
            "runtime_behavior_certified_by_text_search": False}
