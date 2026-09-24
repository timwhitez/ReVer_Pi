"""Budget-indexed descriptive metrics; never infer a completed cost from a prefix.

No statistical population claims and no price lookup. Cached input is a subset
of input. Units are provider-reported tokens, not independent billing telemetry.
"""
from __future__ import annotations
from typing import Any


def counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    total = dict(requests=len(rows), input_tokens=0, cached_input_tokens=0, output_tokens=0)
    for row in rows:
        for name in ("input_tokens", "cached_input_tokens", "output_tokens"):
            value = row[name]
            if type(value) is not int or value < 0:
                raise ValueError("Token counts must be nonnegative integers")
            total[name] += value
        if row["cached_input_tokens"] > row["input_tokens"]:
            raise ValueError("Cached input must be a subset of input")
    total["uncached_input_tokens"] = total["input_tokens"] - total["cached_input_tokens"]
    total["total_tokens"] = total["input_tokens"] + total["output_tokens"]
    return total


def budgeted_outcomes(phases: list[dict[str, Any]], suffix_cap: int) -> dict[str, Any]:
    if type(suffix_cap) is not int or suffix_cap < 1:
        raise ValueError("Positive integer suffix cap required")
    seen = set()
    outcomes = []
    for p in phases:
        name = p["phase"]
        if name not in {"full", "projected"} or name in seen:
            raise ValueError("Exactly one row per named arm")
        seen.add(name)
        used = p["completed_requests"]
        if type(used) is not int or not 0 <= used <= suffix_cap:
            raise ValueError("Completed requests inconsistent with cap")
        state = p["state"]
        exact = p.get("marker_exact")
        reason = p.get("reason")
        if state == "completed":
            if type(exact) is not bool:
                raise ValueError("Completed arm needs an observed answer score")
            success = int(exact)
        elif state == "stopped" and reason == "suffix_cap" and used == suffix_cap:
            if exact is not None:
                raise ValueError("Capped unfinished arm is not an observed incorrect answer")
            success = 0
        else:
            # Infrastructure failures and unvisited arms are not silently zero-filled.
            success = None
        outcomes.append({"phase":name, "state":state, "reason":reason,
                         "completed_requests":used,"completion_by_frozen_cap":success,
                         "observed_final_answer_exact":exact,
                         "unrestricted_completion":exact if state == "completed" else None})
    return {"suffix_cap":suffix_cap,"arms":outcomes,
            "unit":"one_selected_prefix_not_requests","statistical_inference":False,
            "capped_cost_is_completed_task_cost":False}


def price_delta(full: dict[str, int], projected: dict[str, int]) -> dict[str, Any]:
    """Coefficients in token units; multiply by frozen prices per million / 1e6.

    Only interpretable for the stated observation window, not for equal task
    completion when one arm is unfinished. No inferred USD or cancellation proof.
    """
    return {"uncached_input":projected["uncached_input_tokens"]-full["uncached_input_tokens"],
            "cached_input":projected["cached_input_tokens"]-full["cached_input_tokens"],
            "output":projected["output_tokens"]-full["output_tokens"],
            "formula":"(uncached_input*p_uncached + cached_input*p_cached + output*p_output)/1e6",
            "usd":None,"completed_task_saving":None}
