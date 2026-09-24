"""Strict, bounded JSON contracts; no model calls and no answer-guided extraction.

Hashes identify bytes under a trusted-operator model. They are not signatures or
proof of when the operator saw an outcome. Historical scores are never replaced.
"""
from __future__ import annotations
import hashlib
import json
import math
from typing import Any

MAX_ANSWER = 100_000

class ContractError(ValueError):
    pass

def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)

def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        if key in result:
            raise ContractError("duplicate_json_key")
        result[key] = value
    return result

def _constant(_: str) -> None:
    raise ContractError("nonfinite_json_number")

def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)

def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def strict_loads(text: str | bytes) -> Any:
    value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    # Overflowing numeric literals such as 1e999 bypass parse_constant.
    def check(x):
        if isinstance(x, float):
            require(math.isfinite(x), "nonfinite_json_number")
        elif isinstance(x, list):
            for item in x: check(item)
        elif isinstance(x, dict):
            for item in x.values(): check(item)
    check(value)
    return value

def strict_object_v2(answer: str) -> dict[str, Any]:
    """One JSON object, bare or wrapped by one whole Markdown JSON fence.

    Unlike S4c extracted-v1 this never scans inside malformed JSON or drops
    duplicate keys by decoding and re-encoding. Prose/multiple objects are not
    accepted. This is an explicit NEW scoring contract, not a legacy rescore.
    """
    require(type(answer) is str and len(answer) <= MAX_ANSWER, "invalid_answer")
    text = answer.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        require(len(lines) >= 3 and lines[0].strip().lower() in {"```", "```json"}
                and lines[-1].strip() == "```", "invalid_fence")
        require(all(not line.lstrip().startswith("```") for line in lines[1:-1]),
                "multiple_fences")
        text = "\n".join(lines[1:-1]).strip()
    obj = strict_loads(text)
    require(type(obj) is dict, "json_object_required")
    return obj

def score_v2(answer: str | None, expected: dict[str, Any]) -> dict[str, Any]:
    require(type(expected) is dict and expected, "nonempty_gold_required")
    if answer is None:
        return {"contract": "strict_object_v2", "status": "no_final_answer", "correct": False}
    try:
        obj = strict_object_v2(answer)
    except (ValueError, TypeError, RecursionError):
        return {"contract": "strict_object_v2", "status": "invalid_json", "correct": False}
    if set(obj) != set(expected):
        return {"contract": "strict_object_v2", "status": "schema_mismatch", "correct": False}
    fields = {k: canonical(obj[k]) == canonical(expected[k]) for k in expected}
    return {"contract": "strict_object_v2", "status": "scored", "correct": all(fields.values()),
            "fields": fields}

# Retained for descriptive sensitivity analysis ONLY. Never use for new approval.
def legacy_extract_v1(answer: str | None) -> str | None:
    if not isinstance(answer, str):
        return answer
    decoder = json.JSONDecoder()
    for index, char in enumerate(answer):
        if char != "{": continue
        try:
            obj, _ = decoder.raw_decode(answer[index:])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return json.dumps(obj, sort_keys=True)
    return answer
