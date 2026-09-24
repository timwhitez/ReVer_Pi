#!/usr/bin/env python3
"""S4c: the frozen S4 read-only adapter over a separate data root, plus one declared scoring contract.

No adapter logic is copied. Two module globals that name the data root are rebound
to ``research/s4c`` so the frozen runner reads S4c tasks, sources and controller
gold. Every other subcommand is the frozen CLI unchanged.

``--contract strict`` (default): the frozen scorer on the raw final answer.
``--contract extracted``       : the S4c predeclared contract. The first complete
    JSON object anywhere in the answer text is passed to the SAME frozen strict
    scorer. If the text contains no JSON object the original text is scored, so a
    non-JSON answer remains ``invalid_json`` rather than appearing as a missing
    answer. Both ``score`` and ``analyze`` use the selected contract.

``--auditor frozen`` (default) is the frozen auditor. ``--auditor repaired`` adds
the single documented halt classification (see build_repaired_audit.py) so a
trajectory that exhausted the recovery quota can be audited instead of aborting
with JSONDecodeError. Both auditor variants are always reported side by side.

``--record-split development`` (default) or ``--record-split calibration`` applies
to ``analyze`` only. The frozen task schema admits ``split='development'``
exclusively, so a source-disjoint calibration batch cannot label itself inside the
task file. The flag relabels the EXPORTED training record's split field and nothing
else; the run, plan, audit and score artifacts keep saying ``development``. The
source and snapshot disjointness that the calibration gate checks is unaffected.
"""
from __future__ import annotations
import json, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for path in (REPO, REPO / "src", REPO / "scripts", REPO / "research/s4", Path(__file__).resolve().parent):
    sys.path.insert(0, str(path))

from reverpi_sources import contracts, runner  # noqa: E402
import run as s4_run  # noqa: E402  (research/s4/run.py)

contracts.S4 = runner.S4 = Path(__file__).resolve().parent


def extract_json_object(text):
    """Return the first complete JSON object in *text* as a JSON string, else None."""
    if not isinstance(text, str):
        return None
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return json.dumps(obj, sort_keys=True)
    return None


_strict_score_answer = contracts.score_answer


def score_extracted(answer, task, gold):
    normalized = extract_json_object(answer) if isinstance(answer, str) else answer
    return _strict_score_answer(answer if normalized is None else normalized, task, gold)


def score_strict_v2(answer, task, gold):
    """Versioned whole-object/fence contract; do not rewrite historical scores."""
    from research.s5.contracts import strict_object_v2
    if answer is None:return _strict_score_answer(None,task,gold)
    try:obj=strict_object_v2(answer)
    except (ValueError,TypeError,RecursionError):
        return {"status":"invalid_json","correct":False,"fields":None}
    return _strict_score_answer(json.dumps(obj,allow_nan=False),task,gold)


def main() -> int:
    argv = sys.argv[1:]
    # Order-independent shim flags: consume every leading "--flag value" pair.
    while True:
        if argv[:1] == ["--auditor"]:
            auditor, argv = argv[1], argv[2:]
            if auditor not in {"frozen", "repaired"}:raise ValueError("unknown auditor")
            if auditor == "repaired":
                import audit_repaired
                from reverpi_sources import analysis
                analysis.audit = s4_run.audit = audit_repaired.audit
        elif argv[:1] == ["--record-split"]:
            record_split, argv = argv[1], argv[2:]
            if record_split not in {"development", "calibration"}:raise ValueError("unknown record split")
            if record_split == "calibration":
                from reverpi_sources import analysis as analysis_module
                original = analysis_module.analyze

                def relabeled(*args, **kwargs):
                    result = original(*args, **kwargs)
                    record = result.get("candidate_training_record")
                    if record is not None:
                        record["split"] = "calibration"
                    return result

                analysis_module.analyze = s4_run.analyze = relabeled
        elif argv[:1] == ["--contract"]:
            contract, argv = argv[1], argv[2:]
            if contract not in {"strict", "extracted", "strict_object_v2"}:raise ValueError("unknown contract")
            if contract == "extracted":
                from reverpi_sources import analysis
                analysis.score_answer = s4_run.score_answer = score_extracted
            elif contract == "strict_object_v2":
                from reverpi_sources import analysis
                analysis.score_answer = s4_run.score_answer = score_strict_v2
        else:
            break
    if argv[:1] == ["score-extracted"]:  # convenience alias kept for older notes
        s4_run.score_answer = score_extracted
        sys.argv = ["s4c", "score", *argv[1:]]
        return s4_run.main()
    sys.argv = ["s4c", *argv]
    return s4_run.main()


if __name__ == "__main__":
    raise SystemExit(main())
