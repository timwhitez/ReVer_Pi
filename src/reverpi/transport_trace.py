"""Sanitized transport provenance; never log credentials, prompts or exception text."""
from __future__ import annotations
import math
import time
from typing import TYPE_CHECKING
from .util import canonical
if TYPE_CHECKING:
    from .ledger import Ledger


def exception_signature(exc: BaseException) -> list[dict]:
    """Class/errno only: exception messages and URLs may contain secrets."""
    seen: set[int] = set()
    result = []
    while exc is not None and id(exc) not in seen and len(result) < 8:
        seen.add(id(exc))
        item = {"type": type(exc).__name__, "module": type(exc).__module__}
        code = getattr(exc, "errno", None)
        if type(code) is int:
            item["errno"] = code
        result.append(item)
        exc = exc.__cause__ or exc.__context__
    return result


def record_transport_trace(ledger: Ledger, *, attempt_id: int, phase: str,
                           elapsed_seconds: float, status: int | None,
                           complete: bool, exception: list[dict] | None,
                           timeout_config: dict) -> None:
    if phase not in {"await_headers", "read_body", "validate_response"}:
        raise ValueError("Invalid transport phase")
    if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
        raise ValueError("Invalid elapsed time")
    event = {"type":"transport_trace_v1", "attempt_id":attempt_id, "phase":phase,
             "elapsed_seconds":elapsed_seconds,"http_status":status,
             "response_completed":complete,"exception_chain":exception or [],
             "timeouts_seconds":timeout_config,
             "root_cause_certified":False}
    with ledger.db(True) as db:
        db.execute("INSERT INTO audit(event,created) VALUES (?,?)", (canonical(event),time.time()))
