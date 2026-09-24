from __future__ import annotations
import math
import random
from collections import defaultdict


def holm(values: list[float | None]) -> list[float]:
    """Fixed family: missing comparisons retain their slot as p=1."""
    vals = [1.0 if p is None else p for p in values]
    if any(not 0 <= p <= 1 for p in vals):
        raise ValueError("p values outside [0,1]")
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    adjusted = [1.] * len(vals)
    previous = 0.
    for rank, i in enumerate(order):
        previous = max(previous, min(1., (len(vals)-rank)*vals[i]))
        adjusted[i] = previous
    return adjusted


def mcnemar_exact(wins: int, losses: int) -> float:
    n = wins + losses
    return min(1., 2 * sum(math.comb(n, k) for k in range(min(wins, losses)+1)) / 2**n) if n else 1.


def percentile(xs, q):
    a = sorted(xs)
    if not a:
        return None
    index = (len(a)-1)*q
    lo = int(index)
    hi = min(lo+1, len(a)-1)
    return a[lo]*(hi-index) + a[hi]*(index-lo) if hi != lo else a[lo]


def analyze(rows: list[dict], methods: list[str], baseline="full", seed=20260912, n_boot=2000) -> dict:
    """Primary = predetermined repetition 0. Pair only the SAME task set."""
    if baseline not in methods or len(set(methods)) != len(methods):
        raise ValueError("Baseline must be a member of unique declared methods")
    keys = [(r["task_id"], r["method"], r["repeat"]) for r in rows]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate experimental cells cannot be treated as independent samples")
    source_groups = {}
    for r in rows:
        if r["method"] not in methods or type(r["repeat"]) is not int or r["repeat"] < 0:
            raise ValueError("Unexpected method/repetition")
        if r.get("success") is not None and type(r["success"]) is not bool:
            raise ValueError("Success must be a boolean or explicit unknown")
        old = source_groups.setdefault(r["task_id"], r["source_group"])
        if old != r["source_group"]:
            raise ValueError("Paired task changed source cluster between methods")
    main = [r for r in rows if r["repeat"] == 0]
    table = {}
    for method in methods:
        selected = [r for r in main if r["method"] == method]
        n = len(selected)
        successes = sum(r.get("success") is True for r in selected)
        missing = sum(r.get("success") is None for r in selected)
        table[method] = {"n_planned": n, "successes": successes, "missing": missing,
                         "success_identification_interval": [successes/n, (successes+missing)/n] if n else None,
                         "known_tokens": sum(r.get("cost", {}).get("known_tokens", 0) for r in selected),
                         "accounted_tokens": sum(r.get("cost", {}).get("accounted_tokens", 0) for r in selected),
                         "known_usd": sum(r.get("cost", {}).get("known_usd", 0) for r in selected),
                         "accounted_usd": sum(r.get("cost", {}).get("accounted_usd", 0) for r in selected),
                         "unknown_attempts": sum(r.get("cost", {}).get("unknown_attempts",0) for r in selected),
                         "currency_is_fully_known": all(r.get("cost",{}).get("currency_is_fully_known",False) for r in selected),
                         "cost_includes_failed_cells": True}
    indexed = {(r["task_id"], r["method"]): r for r in main}
    comparisons, ps = [], []
    for method in methods:
        if method == baseline:
            continue
        ids = sorted({r["task_id"] for r in main if r["method"] == baseline} &
                     {r["task_id"] for r in main if r["method"] == method})
        lo_sum = hi_sum = 0
        groups = defaultdict(list)
        wins = losses = missing = 0
        for ident in ids:
            a, b = indexed[ident, method], indexed[ident, baseline]
            av, bv = a.get("success"), b.get("success")
            lo_sum += (int(av) if av is not None else 0) - (int(bv) if bv is not None else 1)
            hi_sum += (int(av) if av is not None else 1) - (int(bv) if bv is not None else 0)
            if av is None or bv is None:
                missing += 1
                continue
            d = int(av)-int(bv)
            groups[a["source_group"]].append(d)
            wins += int(d == 1)
            losses += int(d == -1)
        boot = []
        rng = random.Random(seed)
        keys = list(groups)
        if len(keys) >= 2 and not missing:
            for _ in range(n_boot):
                draw = [x for _ in keys for x in groups[rng.choice(keys)]]
                boot.append(sum(draw)/len(draw))
        independent = bool(ids) and len(keys) == len(ids) and not missing
        p = mcnemar_exact(wins, losses) if independent else None
        ps.append(p)
        comparisons.append({"method": method, "baseline": baseline, "paired_tasks": len(ids), "wins": wins, "losses": losses,
                            "missing_pairs": missing, "source_clusters": len(keys),
                            "risk_difference_identification_interval": [lo_sum/len(ids), hi_sum/len(ids)] if ids else None,
                            "cluster_bootstrap_95": [percentile(boot,.025), percentile(boot,.975)] if boot else None,
                            "independent_mcnemar_p": p,
                            "inference_warning": "Few clusters; interval may be unstable" if len(keys)<20 else None})
    for row, p in zip(comparisons, holm(ps)):
        row["fixed_family_holm_p"] = p
    return {"primary_repeat": 0, "table": table, "comparisons": comparisons,
            "note": "Diagnostic checkpoint scores are not real Pi task-completion benchmark results; raw bounds and sampling intervals differ."}
