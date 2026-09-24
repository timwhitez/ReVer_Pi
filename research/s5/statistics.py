"""Conservative Bernoulli bounds and finite-frame partial identification.

A numerical bound does not establish independent sampling, a fixed stopping rule,
complete ascertainment, source-disjointness, or user authorization. Those belong
in a separately checked prospective design. No existing S4 sample is certified.
"""
from __future__ import annotations
import math
from .contracts import require

def binomial_upper(k: int, n: int, alpha: float = 0.05) -> float:
    require(type(k) is int and type(n) is int and 0 <= k <= n, "invalid_binomial_counts")
    require(type(alpha) in (int, float) and math.isfinite(alpha) and 0 < alpha < 1,
            "invalid_alpha")
    if n == 0 or k == n: return 1.0
    if k == 0: return -math.expm1(math.log(alpha) / n)
    coeff = [math.lgamma(n+1)-math.lgamma(j+1)-math.lgamma(n-j+1) for j in range(k+1)]
    def cdf(p):
        logs = [a+j*math.log(p)+(n-j)*math.log1p(-p) for j,a in enumerate(coeff)]
        mx = max(logs)
        return math.exp(mx)*sum(math.exp(v-mx) for v in logs)
    lo, hi = 0.0, 1.0
    for _ in range(90):
        mid = (lo+hi)/2
        if mid <= lo or mid >= hi: break
        if cdf(mid) > alpha: lo = mid
        else: hi = mid
    return hi

def spending_upper(k: int, n: int, alpha: float = 0.05) -> dict:
    """Union-bound sequence with alpha_n=alpha/[n(n+1)], n>=1.

    Under i.i.d. Bernoulli observations, the one-sided intervals cover at every n
    with probability at least 1-alpha. This simple method is conservative and is
    not the efficient mixture boundary of Howard et al. It cannot repair adaptive
    task construction, selective missing outcomes, or changed harm definitions.
    """
    require(type(n) is int and n >= 1, "positive_n_required")
    require(type(alpha) in (int, float) and math.isfinite(alpha) and 0 < alpha < 1,
            "invalid_alpha")
    local = alpha / (n*(n+1))
    require(local > 0, "alpha_underflow")
    return {"upper": binomial_upper(k,n,local), "local_alpha": local,
            "method": "union_bound_alpha_n=alpha/(n*(n+1))", "global_alpha": alpha}

def selected_harm(action: str, full: bool | None, projected: bool | None) -> bool | None:
    require(action in {"full", "projected"}, "invalid_action")
    require((full is None or type(full) is bool) and
            (projected is None or type(projected) is bool), "invalid_quality_label")
    if action == "full" or full is False or projected is True:
        return False
    if full is True and projected is False:
        return True
    return None

def group_identification(frame: dict[str, list[str]], observations: list[dict]) -> dict:
    """Preserve every predeclared group/task, including absent observations.

    Each observation names one declared task, its policy action, and bounded
    completion outcomes. An unfinished-at-cap branch may be false for that
    frozen endpoint, but missing/transport-unknown branches remain None. This
    returns identification intervals, NOT a population confidence interval.
    """
    require(type(frame) is dict and bool(frame), "nonempty_frame_required")
    owner = {}
    for group, tasks in frame.items():
        require(type(group) is str and group and type(tasks) is list and bool(tasks), "invalid_group")
        for task in tasks:
            require(type(task) is str and task and task not in owner, "duplicate_task")
            owner[task] = group
    values = {t: None for t in owner}; seen = set()
    for row in observations:
        require(type(row) is dict and set(row)=={"task_id","source_group","action","full","projected"},
                "invalid_observation_fields")
        task = row["task_id"]
        require(task in owner and task not in seen and row["source_group"] == owner[task], "frame_mismatch")
        seen.add(task); values[task] = selected_harm(row["action"],row["full"],row["projected"])
    groups = {}
    for group, tasks in frame.items():
        v = [values[t] for t in tasks]
        groups[group] = True if any(x is True for x in v) else None if any(x is None for x in v) else False
    n = len(groups); low = sum(v is True for v in groups.values()); high = sum(v is not False for v in groups.values())
    return {"groups":groups,"n":n,"harm_lower_count":low,"harm_upper_count":high,
            "unknown_groups":high-low,"identification_interval":[low/n,high/n],
            "observed_tasks":len(seen),"planned_tasks":len(owner),
            "population_confidence_claim":False}
