"""Read-only accounting and symptom audits. Never repair or overwrite official results."""
from __future__ import annotations
import contextlib
import hashlib
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@contextlib.contextmanager
def readonly_db(path: Path, *, immutable_snapshot: bool = False) -> Iterator[sqlite3.Connection]:
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("Expected an existing SQLite file, not a directory")
    # Normal SQLite read-only connections can still create WAL/SHM coordination
    # files. Use immutable mode ONLY for an explicitly frozen, checkpointed copy.
    # Never ignore uncheckpointed WAL or a hot rollback journal.
    if type(immutable_snapshot) is not bool:
        raise ValueError("immutable_snapshot must be boolean")
    if immutable_snapshot and any(Path(str(path)+suffix).exists() and
            Path(str(path)+suffix).stat().st_size > 0 for suffix in ("-wal", "-journal")):
        raise ValueError("Immutable audit requires a checkpointed snapshot without pending WAL/journal")
    uri = path.as_uri() + ("?mode=ro&immutable=1" if immutable_snapshot else "?mode=ro")
    db = sqlite3.connect(uri, uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")  # One consistent snapshot even when writers are active.
        yield db
    finally:
        db.rollback()
        db.close()


def _count(value: object, name: str, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def audit_ledger(path: Path, *, immutable_snapshot: bool = False) -> dict:
    """Read an existing gateway OR study ledger without a StudyConfig or writes.

    Unknown attempts are not zero usage. Their reservations are planning charges,
    NOT a certified upper bound when the upstream provider ignores output caps.
    USD is deliberately unavailable: ledger integers do not prove frozen prices.
    """
    groups: dict[str, dict] = {}
    with readonly_db(path, immutable_snapshot=immutable_snapshot) as db:
        fields = {row[1] for row in db.execute("PRAGMA table_info(attempts)")}
        if not {"cell", "state", "actual_tokens", "reserve_tokens"} <= fields:
            raise ValueError("Not a compatible attempts ledger")
        for row in db.execute("SELECT cell,state,actual_tokens,reserve_tokens FROM attempts ORDER BY id"):
            actual = _count(row["actual_tokens"], "actual_tokens", nullable=True)
            reserve = _count(row["reserve_tokens"], "reserve_tokens")
            cell = row["cell"]
            if not isinstance(cell, str) or not isinstance(row["state"], str):
                raise ValueError("Corrupt ledger identifiers")
            g = groups.setdefault(cell, {"cell": cell, "attempts": 0, "observed_tokens": 0,
                "unknown_attempts": 0, "unknown_reserved_tokens": 0, "accounted_tokens": 0,
                "observed_over_reservation_attempts": 0, "states": Counter()})
            g["attempts"] += 1
            g["states"][row["state"]] += 1
            if actual is None:
                g["unknown_attempts"] += 1
                g["unknown_reserved_tokens"] += reserve
            else:
                g["observed_tokens"] += actual
                g["observed_over_reservation_attempts"] += int(actual > reserve)
            g["accounted_tokens"] += reserve if actual is None else actual
    keys = ["attempts", "observed_tokens", "unknown_attempts", "unknown_reserved_tokens",
            "accounted_tokens", "observed_over_reservation_attempts"]
    totals = {k: sum(g[k] for g in groups.values()) for k in keys}
    for g in groups.values():
        g["states"] = dict(sorted(g["states"].items()))
    assert totals["accounted_tokens"] == totals["observed_tokens"] + totals["unknown_reserved_tokens"]
    return {"schema": "reverpi.accounting-audit.v1", "database": str(Path(path).resolve()),
        "totals": totals, "cells": sorted(groups.values(), key=lambda x: x["cell"]),
        "usd_cost": None, "price_status": "not_verified", "read_only": True,
        "immutable_snapshot": immutable_snapshot,
        "unknown_reservations_are_verified_upper_bounds": False,
        "notes": ["Observed usage and planning reservations have different evidential status.",
                  "No calls are retried; no official result or accounting row is modified."]}


def audit_sessions(path: Path) -> dict:
    """Counts gateway-side compactions, not native Pi compaction events."""
    with readonly_db(path) as db:
        fields = {r[1] for r in db.execute("PRAGMA table_info(compact_ops)")}
        if not {"session", "state"} <= fields:
            raise ValueError("Not a compatible gateway sessions database")
        rows = [dict(r) for r in db.execute(
            "SELECT session,state,COUNT(*) AS count FROM compact_ops GROUP BY session,state ORDER BY session,state")]
    return {"schema": "reverpi.activation-audit.v1", "gateway_compactions": rows,
        "native_pi_compactions": None,
        "warning": "Absence in this table does not mean native Pi did not compact. Read native session logs separately."}


@dataclass(frozen=True)
class TrialObservation:
    trial_id: str
    official_reward: float | None
    model_calls: int | None
    committed_compactions: int | None
    return_code: int | None = None
    stderr_empty: bool | None = None

    def __post_init__(self):
        if not isinstance(self.trial_id, str) or not self.trial_id:
            raise ValueError("trial_id is required")
        if self.official_reward is not None and (type(self.official_reward) not in {int, float}
                or not math.isfinite(self.official_reward) or not 0 <= self.official_reward <= 1):
            raise ValueError("official_reward must be a finite value in [0,1], or null")
        _count(self.model_calls, "model_calls", nullable=True)
        _count(self.committed_compactions, "committed_compactions", nullable=True)
        if self.return_code is not None and type(self.return_code) is not int:
            raise ValueError("return_code must be an integer or null")
        if self.stderr_empty is not None and type(self.stderr_empty) is not bool:
            raise ValueError("stderr_empty must be boolean or null")


def diagnose_trial(obs: TrialObservation) -> dict:
    symptom = ("no_model_call" if obs.model_calls == 0 else
               "calls_unknown" if obs.model_calls is None else "model_was_called")
    activation = ("not_activated" if obs.committed_compactions == 0 else
                  "not_observed" if obs.committed_compactions is None else "activated")
    return {"trial_id": obs.trial_id, "official_reward": obs.official_reward,
        "symptom": symptom, "compression_activation": activation,
        "cause_status": "unproven", "infrastructure_suspected": obs.model_calls == 0,
        "compression_caused_outcome": None,
        "warning": "A prior PASS, zero calls, or exit code 0 is not a root-cause proof.",
        "official_result_changed": False, "retry_authorized": False}


def compare_reported_scopes(condition_tokens: dict[str, int], gateway_accounted: int) -> dict:
    """Descriptive reconciliation request, not an assertion of lost money."""
    for name, value in condition_tokens.items():
        _count(value, name)
    _count(gateway_accounted, "gateway_accounted")
    total = sum(condition_tokens.values())
    return {"condition_table_tokens": total, "gateway_accounted_tokens": gateway_accounted,
        "difference_pending_scope_reconciliation": gateway_accounted - total,
        "same_scope_verified": False, "difference_is_missing_cost": None}
