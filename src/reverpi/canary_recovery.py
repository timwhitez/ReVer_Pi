"""Bounded exact-recovery verification for a disposable native smoke session.

The canary deliberately supplies a handle. It tests the actual tool transport
and returned bytes, NOT autonomous retrieval choice or task-solving quality.
No archive entry is synthesized just to make this check pass.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from .errors import LabError
from .research_audit import readonly_db
from .util import digest, strict_json_loads


@dataclass(frozen=True)
class RecoveryProbe:
    handle: str
    start: int
    chars: int
    total_chars: int
    expected: str

    def prompt(self) -> str:
        # The expected text/nonce is intentionally NOT present in this prompt.
        return ("Exercise the recover_evidence tool now, using exactly "
                f"handle={self.handle}, start={self.start}, chars={self.chars}. "
                "Do not use read, bash or other tools. After the tool returns, reply only RECOVERED. "
                "This is an interface check, not a request to guess the archived content.")


def select_probe(archive: Path, namespace: str, nonce: str, max_chars: int) -> RecoveryProbe:
    if not isinstance(nonce, str) or not nonce or type(max_chars) is not int or max_chars < len(nonce):
        raise ValueError("Recovery limit cannot contain the canary nonce")
    with readonly_db(archive) as db:
        row = db.execute("SELECT handle,content FROM blobs WHERE namespace=? AND instr(content,?)>0 "
                         "ORDER BY bytes,handle LIMIT 1", (namespace, nonce)).fetchone()
    if row is None:
        raise LabError("acceptance_recovery", "Compaction did not archive the canary evidence")
    handle, content = row["handle"], row["content"]
    if not isinstance(content,str) or digest(content) != handle:
        raise LabError("archive_corruption", "Canary archive content identity mismatch")
    # Include the exact nonce and a small amount of context; use character offsets
    # because this gateway's recovery protocol is explicitly character-indexed.
    start = max(0, content.index(nonce) - min(32, max_chars - len(nonce)))
    chars = min(max_chars, 256, len(content) - start)
    if nonce not in content[start:start+chars]:
        raise LabError("acceptance_recovery", "Canary nonce is outside the bounded recovery span")
    return RecoveryProbe(handle,start,chars,len(content),content[start:start+chars])


def verify_recovery(events: list[dict], probe: RecoveryProbe) -> dict:
    """Verify the real tool-execution event, not the model's final self-report."""
    completions = [e for e in events if e.get("type")=="tool_execution_end"
                   and e.get("toolName")=="recover_evidence"]
    valid=0
    for event in completions:
        result=event.get("result")
        if event.get("isError") is True or not isinstance(result,dict) or result.get("isError") is True:
            continue
        content=result.get("content",[])
        if not isinstance(content,list): continue
        texts=[b.get("text") for b in content if isinstance(b,dict) and b.get("type")=="text"]
        for text in texts:
            if not isinstance(text,str): continue
            try: obj=strict_json_loads(text)
            except (TypeError,ValueError): continue
            if (isinstance(obj,dict) and obj.get("handle")==probe.handle
                and type(obj.get("start")) is int and obj["start"]==probe.start
                and type(obj.get("end")) is int and obj["end"]==probe.start+len(probe.expected)
                and type(obj.get("total_chars")) is int and obj["total_chars"]==probe.total_chars
                and obj.get("text")==probe.expected):
                valid+=1
                break
    if valid<1:
        raise LabError("acceptance_recovery", "No successful exact archive recovery tool result was observed")
    return {"recovery_verified":True,"recovery_tool_calls":len(completions),
            "recovery_exact_results":valid,"recovery_handle":probe.handle,
            "recovery_span_chars":len(probe.expected),"recovery_content_sha":digest(probe.expected),
            "autonomous_retrieval_choice_verified":False}
