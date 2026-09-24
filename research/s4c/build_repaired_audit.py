#!/usr/bin/env python3
"""Build audit_repaired.py from the frozen auditor by three asserted replacements.

Discovered post hoc on real S4c trajectories: when a real model exhausts the
per-cell recovery quota, the harness returns a tool result whose text is the plain
string "ReVer halted: <kind>" with isError false. The frozen auditor parses that
text as JSON at two sites and aborts with JSONDecodeError, so two real trajectories
could not be audited at all.

The repair does exactly one thing: a bounded "ReVer halted: <kind>" string is
classified as a rejected recovery call, which is what the auditor's existing
rejected_recovery_calls counter is for. No other check is altered. The frozen file
is never written to; the generated copy is diffed and hashed by the batch tooling.
"""
from __future__ import annotations
import difflib, hashlib
from pathlib import Path

RESEARCH = Path(__file__).resolve().parents[1]
FROZEN = RESEARCH / "s4/reverpi_sources/audit.py"
TARGET = Path(__file__).resolve().parent / "audit_repaired.py"

HELPER = "\n".join([
    "",
    "",
    "def halt_kind(text):",
    '    """S4c declared classification: a bounded halt string is a rejection, not a receipt."""',
    '    prefix = "ReVer halted: "',
    "    if not isinstance(text, str) or not text.startswith(prefix):",
    "        return None",
    "    kind = text[len(prefix):].strip()",
    '    return kind if 0 < len(kind) <= 64 and all(c.islower() or c == "_" for c in kind) else None',
    "",
    "",
])

NEED_OLD = "def need(ok, message):\n    if not ok: raise ValueError(message)\n"
IMPORT_OLD = "from .contracts import SourcePlan,tree_manifest,load_task,render_prompt\n"
IMPORT_NEW = "from reverpi_sources.contracts import SourcePlan,tree_manifest,load_task,render_prompt\n"
SEARCH_OLD = "                result=raw_results[ident]\n"
SEARCH_NEW = (SEARCH_OLD
              + "                if halt_kind(result['text']) is not None:\n"
              + "                    rejected += 1\n"
              + "                    continue\n")
RECOVER_OLD = "                val = strict_json_loads(raw)\n"
RECOVER_NEW = ("                if halt_kind(raw) is not None:\n"
               + "                    rejected += 1\n"
               + "                    continue\n"
               + RECOVER_OLD)


def replace_once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"frozen auditor no longer matches patch site: {old!r}"
    return text.replace(old, new)


def main() -> int:
    source = FROZEN.read_text()
    patched = replace_once(source, NEED_OLD, NEED_OLD + HELPER)
    patched = replace_once(patched, SEARCH_OLD, SEARCH_NEW)
    patched = replace_once(patched, RECOVER_OLD, RECOVER_NEW)
    patched = replace_once(patched, IMPORT_OLD, IMPORT_NEW)
    TARGET.write_text(patched)
    diff = "".join(difflib.unified_diff(source.splitlines(True), patched.splitlines(True),
                                        "frozen/audit.py", "s4c/audit_repaired.py"))
    changed = sum(1 for line in diff.splitlines()
                  if line[:1] in "+-" and not line.startswith(("+++", "---")))
    (TARGET.parent / "audit_repaired.diff").write_text(diff)
    print(f"frozen sha256  : {hashlib.sha256(source.encode()).hexdigest()}")
    print(f"repaired sha256: {hashlib.sha256(patched.encode()).hexdigest()}")
    print(f"changed lines  : {changed}")
    print(diff)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
