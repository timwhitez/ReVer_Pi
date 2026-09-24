"""The S4c extraction contract must flip the real S4b format failures and change nothing else.

Answer texts are the actual final answers recorded in the S4b live batch.
"""
from __future__ import annotations
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "research/s4c"))
from s4c import extract_json_object, score_extracted  # noqa: E402

CONTROLLER = ROOT / "research/s4/data/controller"

CASES = [
    # (task, recorded final answer, expected strict status, expected extracted status, extracted correct)
    ("click-types",
     '```json\n{\n  "choice": "Alpha",\n  "clamped": 5,\n  "boolean": false,\n'
     '  "tuple_value": ["name", 7],\n  "invalid_integer": "BadParameter"\n}\n```',
     "invalid_json", "scored", True),
    ("packaging-normalization",
     '```json\n{\n  "version": "1.0rc1+abc.1",\n  "release": [1, 0],\n  "prerelease": true,\n'
     '  "below_final": true,\n  "normalized_name": "friendly-bard-name",\n'
     '  "normalized_version": "1"\n}\n```\n\nNotes:\n- extra prose follows',
     "invalid_json", "scored", True),
    ("packaging-compatibility",
     '```json\n{\n  "wheel_name": "my-pkg",\n  "wheel_tags": ["py3-none-any"],\n'
     '  "accepted": [false, true, true, false],\n  "extras": ["fast"],\n'
     '  "marker_results": {"3.11": true, "3.12": false}\n}\n```',
     "invalid_json", "scored", False),
    ("packaging-compatibility",
     '```json\n{\n  "wheel_name": "my-pkg",\n  "wheel_tags": ["py3-none-any"],\n'
     '  "accepted": [false, true, true, false],\n  "extras": ["fast"],\n'
     '  "marker_results": [true, false]\n}\n```',
     "invalid_json", "scored", True),
]


def test_extraction_flips_format_failures_only():
    from reverpi_sources.contracts import load_task, score_answer

    for task_id, answer, strict_status, extracted_status, extracted_ok in CASES:
        task = load_task(ROOT / "research/s4/data/tasks" / f"{task_id}.json")
        gold = json.loads((CONTROLLER / f"{task_id}.gold.json").read_text())
        strict = score_answer(answer, task, gold)
        extracted = score_extracted(answer, task, gold)
        assert strict["status"] == strict_status, (task_id, strict)
        assert extracted["status"] == extracted_status, (task_id, extracted)
        assert extracted["correct"] is extracted_ok, (task_id, extracted)


def test_no_json_object_stays_a_contract_failure():
    from reverpi_sources.contracts import load_task, score_answer

    task = load_task(ROOT / "research/s4/data/tasks/cache-behavior.json")
    gold = json.loads((CONTROLLER / "cache-behavior.gold.json").read_text())
    prose = "I could not determine the values from the supplied source."
    assert score_extracted(prose, task, gold) == score_answer(prose, task, gold)
    assert score_extracted(None, task, gold)["status"] == "no_final_answer"


def test_extraction_is_identity_for_a_bare_object():
    task_bare = '{"a": 1, "b": [true, null]}'
    assert json.loads(extract_json_object(task_bare)) == {"a": 1, "b": [True, None]}
