"""Executable, offline evidence lifecycle primitives; no autonomous tool dispatch.

Byte integrity, observed dependency compatibility, and decision sufficiency are
separate properties. The last one is intentionally not certified by this module.
"""
from __future__ import annotations
import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Literal, Mapping


@dataclass(frozen=True)
class Evidence:
    id: str
    sha256: str  # SHA-256 of raw UTF-8 bytes, distinct from Archive's JSON-content handle.
    dependencies: Mapping[str, str] = field(default_factory=dict)
    verifier: str | None = None

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id or not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("An evidence ID and a lowercase raw-byte SHA-256 are required")
        if any(not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in self.dependencies.items()):
            raise ValueError("Dependency identities must be nonempty strings")
        # The caller cannot subsequently mutate a dependency mapping unnoticed.
        from types import MappingProxyType
        object.__setattr__(self, "dependencies", MappingProxyType(dict(self.dependencies)))
        if self.verifier is not None and (not isinstance(self.verifier, str) or not self.verifier):
            raise ValueError("verifier is a registered identifier, not an empty command")


def evidence_state(evidence: Evidence, current: Mapping[str, str], raw: bytes | None = None) -> dict:
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in current.items()):
        raise ValueError("Current dependency identities must be strings")
    if raw is not None and not isinstance(raw, bytes):
        raise ValueError("Raw evidence must be bytes")
    integrity = "unchecked" if raw is None else ("matches" if hashlib.sha256(raw).hexdigest() == evidence.sha256 else "mismatch")
    if any(k in current and current[k] != v for k, v in evidence.dependencies.items()):
        applicability = "stale"
    elif not evidence.dependencies or any(k not in current for k in evidence.dependencies):
        applicability = "unknown"
    else:
        applicability = "snapshot_matches"
    return {"integrity": integrity, "applicability": applicability,
        "dependency_completeness_certified": False, "decision_sufficiency_certified": False}


def plan_evidence(evidence: Evidence, current: Mapping[str, str], *, raw: bytes | None = None,
                  require_current: bool = True) -> dict:
    if type(require_current) is not bool:
        raise ValueError("require_current must be boolean")
    status = evidence_state(evidence, current, raw)
    if status["integrity"] == "mismatch":
        action = "quarantine"
    elif require_current and status["applicability"] != "snapshot_matches":
        action = "revalidate" if evidence.verifier else "request_new_evidence"
    elif raw is None:
        action = "recover"
    else:
        action = "retain_as_evidence_not_a_proof"
    return {"evidence_id": evidence.id, "action": action, "status": status,
        "verifier_id": evidence.verifier if action == "revalidate" else None,
        "executes_verifier": False}


@dataclass(frozen=True)
class SavingsEstimate:
    # All values must use one frozen unit (e.g. USD or latency), not mixed tokens/USD.
    future_saving_lower: float
    rewrite_upper: float
    recovery_upper: float
    revalidation_upper: float
    cache_rewarm_upper: float
    unit: Literal["usd", "seconds", "tokens"]
    bounds_verified: bool = False

    def __post_init__(self):
        for name in ("future_saving_lower", "rewrite_upper", "recovery_upper", "revalidation_upper", "cache_rewarm_upper"):
            value = getattr(self, name)
            if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.unit not in {"usd", "seconds", "tokens"} or type(self.bounds_verified) is not bool:
            raise ValueError("Invalid unit or bounds flag")

    @property
    def net_lower(self) -> float:
        value = self.future_saving_lower - self.rewrite_upper - self.recovery_upper - self.revalidation_upper - self.cache_rewarm_upper
        if not math.isfinite(value):
            raise ValueError("Derived cost overflow")
        return value


def economic_gate(estimate: SavingsEstimate, *, calibrated_harm_upper: float | None,
                  harm_tolerance: float, representation_fits: bool, evidence_contract_passed: bool) -> dict:
    if type(harm_tolerance) not in {int, float} or not math.isfinite(harm_tolerance) or not 0 <= harm_tolerance < 1:
        raise ValueError("harm_tolerance must be in [0,1)")
    if calibrated_harm_upper is not None and (type(calibrated_harm_upper) not in {int, float}
            or not math.isfinite(calibrated_harm_upper) or not 0 <= calibrated_harm_upper <= 1):
        raise ValueError("Invalid calibrated risk bound")
    if type(representation_fits) is not bool or type(evidence_contract_passed) is not bool:
        raise ValueError("Contract flags must be boolean")
    if not representation_fits:
        reason = "capacity_requires_explicit_fallback"
    elif not evidence_contract_passed:
        reason = "evidence_contract_failed"
    elif calibrated_harm_upper is None:
        reason = "uncalibrated"
    elif calibrated_harm_upper > harm_tolerance:
        reason = "risk_above_tolerance"
    elif not estimate.bounds_verified:
        reason = "economic_bounds_unverified"
    elif estimate.net_lower <= 0:
        reason = "nonpositive_net_saving"
    else:
        reason = "admissible_under_supplied_assumptions"
    return {"compress": reason == "admissible_under_supplied_assumptions", "reason": reason,
        "net_saving_lower": estimate.net_lower, "unit": estimate.unit,
        "deployment_guarantee": False, "note": "Calibration shift and incomplete dependencies invalidate stronger claims."}
