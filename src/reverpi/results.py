"""Frozen-grid result integrity and current-ledger cost views.

Embedded hashes detect accidental/manual edits and torn artifacts, not a hostile
operator who can rewrite the code, manifest and every hash. Preserve original
runs as evidence; do not convert an old schema into a certified run by hashing it.
"""
from __future__ import annotations
import copy
import json
import re
from pathlib import Path
from .config import StudyConfig
from .errors import LabError
from .ledger import Ledger
from .util import atomic_write, bytes_digest, canonical, digest, strict_json_loads

KEYS = ("cell", "task_id", "source_group", "method", "repeat")
TRACKS = {"checkpoint_diagnostics_NOT_Pi_benchmark", "native_pi_harbor"}


def write_results(path: Path, rows: list[dict]) -> None:
    """Row seals and scores share ONE atomic write; no two-file commit window."""
    for row in rows:
        row["row_sha256"] = digest({k:v for k,v in row.items() if k != "row_sha256"})
    atomic_write(path, canonical(rows))


def read_results(run: Path, manifest: dict | None = None) -> list[dict]:
    if manifest is None:
        manifest = strict_json_loads((run / "manifest.json").read_bytes())
    if not isinstance(manifest, dict):
        raise LabError("manifest_schema", "Manifest must be an object")
    rows = strict_json_loads((run / "results.json").read_bytes())
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise LabError("result_changed", "Saved result grid is not an array of objects")
    # Explicit ad-hoc inputs to pure statistics helpers are not certified runs.
    # Actual framework tracks require the new identity/row-seal contract.
    if manifest.get("track") not in TRACKS:
        return rows
    if manifest.get("results_schema") != 2:
        raise LabError("result_schema", "Legacy run: preserve it and use its original analyzer; do not silently re-certify results")
    expected = manifest.get("result_grid", manifest.get("grid"))
    if not isinstance(expected, list) or any(not isinstance(e, dict) for e in expected) or len(rows) != len(expected):
        raise LabError("grid_changed", "Result denominator differs from the frozen grid")
    ids = [r.get("cell") for r in rows]
    if any(not isinstance(x, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", x) is None for x in ids) or len(set(ids)) != len(ids):
        raise LabError("grid_changed", "Duplicate or malformed result cells")
    for r, e in zip(rows, expected):
        if any(r.get(k) != e.get(k) for k in KEYS):
            raise LabError("grid_changed", "Result order or task/condition identity differs from the frozen grid")
        if r.get("row_sha256") != digest({k:v for k,v in r.items() if k != "row_sha256"}):
            raise LabError("result_changed", "Result row changed after atomic commit")
        if r.get("status") == "complete":
            if type(r.get("success")) is not bool:
                raise LabError("result_changed", "Completed result must have a Boolean outcome")
            if manifest["track"] == "checkpoint_diagnostics_NOT_Pi_benchmark":
                p = run / "answers" / (r["cell"] + ".json")
                if (p.is_symlink() or p.parent.is_symlink() or not p.is_file()
                        or not p.resolve().is_relative_to((run/"answers").resolve())
                        or r.get("answer_sha") != bytes_digest(p.read_bytes())):
                    raise LabError("result_changed", "Diagnostic answer artifact changed or is missing")
            else:
                p = _frozen_absolute_path(r.get("official_result_path"), "official_result_path")
                if not p.is_file() or r.get("official_result_sha") != bytes_digest(p.read_bytes()):
                    raise LabError("result_changed", "Authoritative Harbor result changed or is missing")
    return rows


def _frozen_absolute_path(value, field: str) -> Path:
    # Native writers have always frozen resolved absolute paths. Never guess a
    # relocated artifact from process CWD or silently change historical identity.
    if not isinstance(value, str) or not value or "\0" in value or not Path(value).is_absolute():
        raise LabError("manifest_schema", f"{field} must be a nonempty frozen absolute path")
    return Path(value)


def current_cost_view(run: Path, manifest: dict, rows: list[dict]) -> tuple[list[dict], str]:
    """Read current reservations/reconciliations without altering sealed outcomes."""
    if not isinstance(manifest, dict):
        raise LabError("manifest_schema", "Manifest must be an object")
    if manifest.get("track") not in TRACKS:
        return rows, "ad_hoc_snapshot_unverified"
    native = manifest["track"] == "native_pi_harbor"
    required = ("gateway_study", "plan_id", "gateway_run") if native else ("provider", "study", "public_sha", "gold_sha", "code_sha")
    if any(k not in manifest or manifest[k] is None for k in required):
        raise LabError("manifest_schema", "Missing frozen cost identity fields")
    try:
        cfg = StudyConfig.model_validate(manifest["gateway_study"] if native else manifest["study"])
    except ValueError as exc:
        raise LabError("manifest_schema", "Invalid frozen study configuration") from exc
    location = _frozen_absolute_path(manifest.get("gateway_run"), "gateway_run") if native else run
    path = location / "ledger.sqlite"
    if not path.is_file():
        raise LabError("ledger_missing", "Current cost evidence is unavailable; do not report stale snapshots as reconciled costs")
    ledger = Ledger(path, cfg.budget)
    # A different ledger with the same limits is NOT sufficient provenance.
    with ledger.db() as db:
        if native:
            bound = db.execute("SELECT value FROM meta WHERE key='benchmark_plan'").fetchone()
            if not bound or json.loads(bound[0]) != manifest["plan_id"]:
                raise LabError("ledger_identity", "Native ledger does not belong to this frozen plan")
        else:
            bound = db.execute("SELECT value FROM meta WHERE key='study'").fetchone()
            identity = {k: manifest[k] for k in ("provider", "study", "public_sha", "gold_sha", "code_sha")}
            if not bound or json.loads(bound[0]) != identity:
                raise LabError("ledger_identity", "Cost ledger does not belong to this diagnostic run")
    fresh = copy.deepcopy(rows)
    for row in fresh:
        row["cost"] = ledger.totals(row["cell"])
    return fresh, "current_ledger_including_reconciliations"
