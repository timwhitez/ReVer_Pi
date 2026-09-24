from __future__ import annotations
from dataclasses import dataclass
from typing import Any


@dataclass
class LabError(Exception):
    kind: str
    message: str
    retryable: bool = False
    ambiguous: bool = False
    status: int | None = None

    def __str__(self):
        return f"{self.kind}: {self.message}"

    def record(self):
        return {"kind": self.kind, "message": self.message, "retryable": self.retryable,
                "ambiguous": self.ambiguous, "status": self.status}


def classify_http(status: int, body: Any) -> LabError:
    # Never persist vendor messages: gateways sometimes echo the original prompt/key.
    e = body.get("error", {}) if isinstance(body, dict) else {}
    code = str(e.get("code", e.get("type", ""))) if isinstance(e, dict) else ""
    msg = str(e.get("message", "")) if isinstance(e, dict) else ""
    check = (code + " " + msg).lower()
    if status in {401, 403}:
        kind = "authentication" if status == 401 else "permission"
    elif "insufficient_quota" in check or "quota_exceeded" in check or "余额" in check or status == 402:
        kind = "quota"
    elif status == 429:
        return LabError("rate_limit", "Provider throttled request", True, False, status)
    elif status in {408, 409, 425} or status >= 500:
        return LabError("upstream_transient", "Provider returned a transient status; billing may be unknown", True, True, status)
    elif status == 404:
        kind = "endpoint_or_model"
    elif status == 413 or any(x in check for x in ("context_length", "context window", "maximum context", "too many tokens")):
        kind = "context_overflow"
    elif status in {400, 422}:
        kind = "invalid_request"
    elif status in {301, 302, 307, 308}:
        kind = "redirect_rejected"
    else:
        kind = "http_error"
    return LabError(kind, f"HTTP {status}; inspect provider configuration/request ID, not automatic parameter deletion", False, False, status)
