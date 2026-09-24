"""Exact one-sided binomial harm bound for independent, preselected clusters.

This is an offline calibration aid, NOT an anytime-valid test or a formal safety
guarantee. Frozen policies only. Repeated trials of one task are NOT new clusters.
"""
from __future__ import annotations
import math
from dataclasses import dataclass


def _binom_cdf(k: int, n: int, p: float) -> float:
    if p <= 0:
        return 1.0
    if p >= 1:
        return float(k >= n)
    logq, logp = math.log1p(-p), math.log(p)
    log_terms = [math.lgamma(n+1)-math.lgamma(i+1)-math.lgamma(n-i+1)+i*logp+(n-i)*logq
                 for i in range(k+1)]
    top = max(log_terms)
    return min(1.0, math.exp(top) * math.fsum(math.exp(v-top) for v in log_terms))


def harm_upper_bound(harms: int, clusters: int, *, alpha: float = .05, policies: int = 1) -> float:
    if type(clusters) is not int or type(harms) is not int or not 0 <= harms <= clusters <= 10000:
        raise ValueError("Require 0 <= harms <= clusters <= 10000")
    if type(policies) is not int or policies < 1:
        raise ValueError("policies must be a positive integer")
    if type(alpha) not in {int, float} or not math.isfinite(alpha) or not 0 < alpha < 1:
        raise ValueError("alpha must be in (0,1)")
    corrected = alpha / policies
    if corrected == 0:
        raise ValueError("Family-wise alpha underflow")
    if clusters == 0 or harms == clusters:
        return 1.0
    if harms == 0:
        return -math.expm1(math.log(corrected) / clusters)
    lo, hi = harms / clusters, 1.0
    for _ in range(65):
        mid = (lo+hi)/2
        if _binom_cdf(harms, clusters, mid) > corrected:
            lo = mid
        else:
            hi = mid
    return hi


@dataclass(frozen=True)
class PairedCluster:
    cluster_id: str
    baseline_success: bool
    candidate_success: bool
    split: str = "calibration"

    def __post_init__(self):
        if not isinstance(self.cluster_id, str) or not self.cluster_id:
            raise ValueError("A nonempty independent source-cluster ID is required")
        if type(self.baseline_success) is not bool or type(self.candidate_success) is not bool:
            raise ValueError("Success labels must be boolean")
        if self.split != "calibration":
            raise ValueError("Only isolated calibration data can calibrate this policy")


def calibrate(rows: list[PairedCluster], *, alpha: float = .05, policies: int = 1) -> dict:
    ids = [r.cluster_id for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate clusters: predeclare one pair per cluster or aggregate before calibration")
    harms = sum(r.baseline_success and not r.candidate_success for r in rows)
    wins = sum(not r.baseline_success and r.candidate_success for r in rows)
    n = len(rows)
    return {"clusters": n, "harms": harms, "wins": wins,
        "harm_upper": harm_upper_bound(harms, n, alpha=alpha, policies=policies),
        "alpha_familywise": alpha, "frozen_policies": policies,
        "quality_difference": (wins-harms)/n if n else None,
        "scope": "Unconditional paired harm probability, not a conditional success-cost estimate.",
        "assumptions": ["One independent draw per source cluster", "Policies fixed before these labels are inspected",
            "One fixed-sample analysis", "Future deployment follows the calibration distribution"],
        "external_evaluation_replaced": False}
