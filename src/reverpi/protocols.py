"""Text/function-tool protocols. Opaque Responses items are replayed verbatim.

Images, hosted tools, cross-provider reasoning handoff and stateful response IDs
are intentionally rejected rather than silently flattened or lossy-translated.
"""
from __future__ import annotations
import copy
import json
from dataclasses import asdict, dataclass, field
from typing import Any
from .config import Provider
from .errors import LabError
from .util import canonical, strict_json_loads


@dataclass
class Message:
    role: str
    content: str = ""
    calls: list[dict] = field(default_factory=list)
    call_id: str | None = None
    reasoning: str | None = None
    response_items: list[dict] | None = None

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            raise ValueError("Canonical message must be an object")
        allowed = {"role", "content", "calls", "call_id", "reasoning", "response_items"}
        if set(d) - allowed:
            raise ValueError("Unknown canonical message fields")
        return cls(**d)


@dataclass
class Completion:
    text: str
    calls: list[dict]
    reasoning: str | None
    response_items: list[dict] | None
    usage: dict | None
    model: str
    response_id: str | None
    stop: str
    raw: dict

    def message(self) -> Message:
        return Message("assistant", self.text, self.calls, reasoning=self.reasoning, response_items=self.response_items)

    def to_dict(self):
        return asdict(self)


def validate_messages(messages: list[Message]):
    pending: set[str] = set()
    seen: set[str] = set()
    if not messages:
        raise LabError("protocol", "Empty message array")
    for m in messages:
        if not isinstance(m, Message) or not isinstance(m.role, str) or not isinstance(m.calls, list):
            raise LabError("message_schema", "Invalid canonical message/call list")
        if m.call_id is not None and not isinstance(m.call_id, str):
            raise LabError("message_schema", "Tool call_id must be text")
        try:
            if isinstance(m.content, str): m.content.encode("utf-8")
            if isinstance(m.reasoning, str): m.reasoning.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise LabError("unsupported_content", "Canonical messages must be valid UTF-8") from exc
        if m.reasoning is not None and not isinstance(m.reasoning, str):
            raise LabError("message_schema", "Reasoning state must be text")
        if m.response_items is not None and (not isinstance(m.response_items, list) or any(not isinstance(x, dict) for x in m.response_items)):
            raise LabError("message_schema", "Opaque response state must be an item array")
        if m.role != "assistant" and (m.reasoning is not None or m.response_items is not None or m.calls):
            raise LabError("message_schema", "Only an assistant may carry reasoning or response state")
        if m.role != "tool" and m.call_id is not None:
            raise LabError("message_schema", "Only a tool result may have a call_id")
        if m.role not in {"system", "developer", "user", "assistant", "tool"} or not isinstance(m.content, str):
            raise LabError("unsupported_content", "Only canonical text and function-tool messages are supported")
        if m.role == "tool":
            if not m.call_id or m.call_id not in pending:
                raise LabError("tool_pairing", "Tool result has no unmatched call")
            pending.remove(m.call_id)
        else:
            if pending:
                raise LabError("tool_pairing", "Missing tool result before next non-tool message")
            if m.calls and m.role != "assistant":
                raise LabError("tool_pairing", "Only assistant messages may call tools")
            for c in m.calls:
                if not isinstance(c, dict) or set(c) != {"id", "name", "arguments"} or not isinstance(c["arguments"], dict):
                    raise LabError("tool_arguments", "Invalid canonical function call")
                if not isinstance(c["id"], str) or not isinstance(c["name"], str) or not c["name"] or not c["id"] or c["id"] in seen:
                    raise LabError("tool_pairing", "Duplicate/empty tool call ID")
                seen.add(c["id"])
                pending.add(c["id"])
    if pending:
        raise LabError("tool_pairing", "Cannot dispatch while tool calls remain unresolved")


def build_request(p: Provider, messages: list[Message], tools: list[dict] | None = None,
                  *, effort: str | None = None, max_output_tokens: int | None = None) -> tuple[str, dict]:
    validate_messages(messages)
    requested = p.effort if effort is None else effort
    if requested not in p.effort_map:
        raise LabError("unsupported_effort", "No explicit mapping; reasoning will not be disabled as fallback")
    mapped = p.effort_map[requested]
    maximum = p.max_output_tokens if max_output_tokens is None else max_output_tokens
    if type(maximum) is not int or not 1 <= maximum <= p.max_output_tokens:
        raise LabError("output_budget", "Requested output limit exceeds provider profile")
    tools = [] if tools is None else tools
    if not isinstance(tools, list):
        raise LabError("tool_schema", "Tools must be a list")
    names = set()
    for t in tools:
        if not isinstance(t, dict) or set(t) != {"name", "description", "parameters"} or not isinstance(t["parameters"], dict):
            raise LabError("tool_schema", "Canonical tools require name, description, parameters")
        if not isinstance(t["name"], str) or not t["name"] or t["name"] in names or not isinstance(t["description"], str):
            raise LabError("tool_schema", "Tool names must be nonempty/unique and descriptions textual")
        names.add(t["name"])
        try:
            import jsonschema
            jsonschema.Draft202012Validator.check_schema(t["parameters"])
        except jsonschema.SchemaError as exc:
            raise LabError("tool_schema", "Invalid JSON Schema; tool was not dispatched") from exc
    body = copy.deepcopy(p.extra_body)
    body.update(model=p.model, stream=p.stream)
    if p.protocol == "chat_completions":
        out = []
        for m in messages:
            if m.response_items:
                raise LabError("cross_protocol_state", "Responses state cannot be replayed through Chat Completions")
            row: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.role == "tool":
                row["tool_call_id"] = m.call_id
            if m.role == "assistant":
                if m.reasoning is not None:
                    row["reasoning_content"] = m.reasoning
                if m.calls:
                    row["tool_calls"] = [{"id": c["id"], "type": "function", "function":
                                           {"name": c["name"], "arguments": canonical(c["arguments"])}} for c in m.calls]
            out.append(row)
        body.update(messages=out)
        body[p.chat_output_field] = maximum
        if p.reasoning_style != "thinking_only":
            body["reasoning_effort"] = mapped
        if p.reasoning_style in {"thinking", "thinking_only"}:
            body["thinking"] = {"type": "enabled"}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        if p.stream and p.stream_options_usage:
            body["stream_options"] = {"include_usage": True}
        return "/chat/completions", body
    items = []
    for m in messages:
        if m.role == "assistant" and m.response_items is not None:
            # Validate the visible projection before replaying opaque state verbatim.
            # Otherwise canonical tool results could be paired with different actions.
            if m.reasoning is not None:
                raise LabError("cross_protocol_state", "Chat reasoning requires its original protocol")
            replay_text, replay_calls = _response_content(m.response_items)
            if replay_text != m.content or replay_calls != m.calls:
                raise LabError("response_state_mismatch", "Opaque Responses state disagrees with canonical content/calls")
            items.extend(copy.deepcopy(m.response_items))
        elif m.role == "tool":
            items.append({"type": "function_call_output", "call_id": m.call_id, "output": m.content})
        else:
            if m.reasoning is not None:
                raise LabError("cross_protocol_state", "Chat reasoning requires its original protocol")
            if m.content:
                items.append({"role": m.role, "content": m.content})
            for c in m.calls:
                items.append({"type": "function_call", "call_id": c["id"], "name": c["name"], "arguments": canonical(c["arguments"])})
    body.update(input=items, reasoning={"effort": mapped}, max_output_tokens=maximum, store=False)
    if p.responses_include_encrypted_reasoning:
        body["include"] = ["reasoning.encrypted_content"]
    if tools:
        # Preserve optional properties: Responses may otherwise normalize to strict.
        body["tools"] = [{"type": "function", **t, "strict": False} for t in tools]
    return "/responses", body


def _args(s: Any) -> dict:
    try:
        v = strict_json_loads(s) if isinstance(s, str) else s
    except (ValueError, TypeError, RecursionError) as exc:
        raise LabError("tool_arguments", "Provider returned malformed function arguments") from exc
    if not isinstance(v, dict):
        raise LabError("tool_arguments", "Function arguments must be a JSON object")
    try:
        canonical(v).encode("utf-8")  # Reject non-finite values and invalid Unicode before tools execute.
    except (ValueError, TypeError, RecursionError) as exc:
        raise LabError("tool_arguments", "Function arguments contain non-JSON values") from exc
    return v


def _response_content(items: Any) -> tuple[str, list[dict]]:
    if not isinstance(items, list):
        raise LabError("malformed_response", "Responses output must be an array")
    text, calls = "", []
    for item in items:
        if not isinstance(item, dict):
            raise LabError("malformed_response", "Responses output item must be an object")
        typ = item.get("type")
        if "status" in item and item["status"] != "completed":
            raise LabError("output_incomplete", "Responses item is not completed")
        if typ == "message":
            if "role" in item and item["role"] != "assistant":
                raise LabError("malformed_response", "Expected an assistant output message")
            parts = item.get("content")
            if not isinstance(parts, list):
                raise LabError("malformed_response", "Responses message content must be an array")
            for part in parts:
                if not isinstance(part, dict):
                    raise LabError("malformed_response", "Response content part must be an object")
                if part.get("type") == "refusal":
                    raise LabError("refusal", "Provider refused the request")
                if part.get("type") != "output_text":
                    raise LabError("unsupported_content", "Unknown Responses message content")
                if not isinstance(part.get("text"), str):
                    raise LabError("unsupported_content", "Response text must be a string")
                text += part["text"]
        elif typ == "function_call":
            if not {"call_id", "name", "arguments"}.issubset(item):
                raise LabError("tool_arguments", "Missing function call fields")
            calls.append({"id": item["call_id"], "name": item["name"], "arguments": _args(item["arguments"])})
        elif typ not in ("reasoning", "compaction"):
            raise LabError("unsupported_tool", "Hosted/unknown Responses item is outside this experiment")
    _validate_calls(calls)
    return text, calls


def _validate_calls(calls: list[dict]) -> None:
    if any(not isinstance(x["id"], str) or not x["id"] or
           not isinstance(x["name"], str) or not x["name"] for x in calls):
        raise LabError("tool_pairing", "Generated call identifiers and names must be nonempty strings")
    ids = [x["id"] for x in calls]
    if len(ids) != len(set(ids)):
        raise LabError("tool_pairing", "Duplicate generated call IDs")


def parse_completion(p: Provider, raw: dict) -> Completion:
    if not isinstance(raw, dict) or raw.get("error"):
        raise LabError("response_error", "Error object in successful HTTP response", ambiguous=True)
    text, calls, reasoning, items = "", [], None, None
    if p.protocol == "chat_completions":
        choices = raw.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise LabError("malformed_response", "Expected exactly one Chat choice object", ambiguous=True)
        ch = choices[0]
        if "index" in ch and (type(ch["index"]) is not int or ch["index"] != 0):
            raise LabError("multiple_choices", "Expected choice index zero")
        stop, m = ch.get("finish_reason"), ch.get("message")
        if not isinstance(m, dict) or ("role" in m and m["role"] != "assistant"):
            raise LabError("malformed_response", "Expected an assistant message object")
        if stop == "length":
            raise LabError("output_incomplete", "Output limit reached; partial completion is not committed")
        if stop == "content_filter" or m.get("refusal"):
            raise LabError("refusal", "Provider refused the request")
        if not isinstance(stop, str) or stop not in {"stop", "tool_calls"}:
            raise LabError("missing_terminal", "No supported terminal finish reason", ambiguous=True)
        text = m.get("content")
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise LabError("unsupported_content", "Provider returned non-text content")
        reasoning = m.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            raise LabError("malformed_response", "reasoning_content must be text")
        tool_calls = m.get("tool_calls")
        if tool_calls is None:
            tool_calls = []
        if not isinstance(tool_calls, list):
            raise LabError("malformed_response", "tool_calls must be an array")
        for c in tool_calls:
            if not isinstance(c, dict):
                raise LabError("malformed_response", "Tool call must be an object")
            if c.get("type") != "function":
                raise LabError("unsupported_tool", "Only function tools are supported")
            f = c.get("function")
            if "id" not in c or not isinstance(f, dict) or not {"name", "arguments"}.issubset(f):
                raise LabError("tool_arguments", "Missing function call fields")
            calls.append({"id": c["id"], "name": f["name"], "arguments": _args(f["arguments"])})
        if (stop == "tool_calls") != bool(calls):
            raise LabError("tool_pairing", "Finish reason and complete tool calls disagree")
    else:
        if raw.get("status") != "completed":
            raise LabError("output_incomplete", "Responses status is not completed; no partial state is committed")
        items = raw.get("output")
        text, calls = _response_content(items)
        stop = "tool_calls" if calls else "stop"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LabError("unsupported_content", "Output text is not valid UTF-8") from exc
    if not text.strip() and not calls:
        raise LabError("empty_completion", "No visible answer or complete tool call; reasoning alone is not a result")
    _validate_calls(calls)
    model = raw.get("model")
    if not isinstance(model, str) or not model:
        raise LabError("missing_identity", "Provider omitted returned model identity")
    response_id = raw.get("id")
    if response_id is not None and (not isinstance(response_id, str) or not response_id):
        raise LabError("missing_identity", "Invalid response identity")
    return Completion(text, calls, reasoning, items, raw.get("usage"), model, response_id, stop, raw)


def usage_parts(raw: Any) -> tuple[int, int, int] | None:
    """Validate accounting counters; inconsistent or malformed usage stays unknown."""
    if not isinstance(raw, dict):
        return None
    inp = raw.get("input_tokens", raw.get("prompt_tokens"))
    out = raw.get("output_tokens", raw.get("completion_tokens"))
    if any(type(v) is not int or v < 0 for v in (inp, out)):
        return None
    for key, expected in (("input_tokens", inp), ("prompt_tokens", inp),
                          ("output_tokens", out), ("completion_tokens", out),
                          ("total_tokens", inp + out)):
        if key in raw and (type(raw[key]) is not int or raw[key] != expected):
            return None
    # Validate EVERY present dialect alias, not only the first non-null one.
    # Contradictory nested details must not become a cheaper known bill.
    cached_values = []
    reasoning_values = []
    for key, field, ceiling, target in (
        ("input_tokens_details", "cached_tokens", inp, cached_values),
        ("prompt_tokens_details", "cached_tokens", inp, cached_values),
        ("output_tokens_details", "reasoning_tokens", out, reasoning_values),
        ("completion_tokens_details", "reasoning_tokens", out, reasoning_values),
    ):
        detail = raw.get(key)
        if detail is None:
            continue
        if not isinstance(detail, dict):
            return None
        if field in detail:
            value = detail[field]
            if type(value) is not int or not 0 <= value <= ceiling:
                return None
            target.append(value)
    cached = cached_values[0] if cached_values else raw.get("prompt_cache_hit_tokens", 0)
    if type(cached) is not int or not 0 <= cached <= inp or any(v != cached for v in cached_values):
        return None
    if reasoning_values and any(v != reasoning_values[0] for v in reasoning_values):
        return None
    for key, expected in (("prompt_cache_hit_tokens", cached), ("prompt_cache_miss_tokens", inp - cached)):
        if key in raw and (type(raw[key]) is not int or raw[key] != expected):
            return None
    return inp, out, cached


def normalize_usage(raw: dict | None, prices) -> tuple[int | None, float | None]:
    parts = usage_parts(raw)
    if parts is None:
        return None, None
    inp, out, cached = parts
    # Reasoning tokens are a subset of output, not additional billable output.
    usd = ((inp - cached) * prices.input_per_million + cached * prices.cached_input_per_million
           + out * prices.output_per_million) / 1_000_000 if prices.configured else None
    return inp + out, usd


def conservative_input_tokens(payload: dict) -> int:
    """Byte-level planning bound for text/function schemas, NOT a vendor tokenizer.

Opaque native compaction uses a separately configured reservation. Token usage is
ultimately recorded from the provider. Exceeding this estimate latches a stop.
    """
    return len(canonical(payload).encode("utf-8")) + 256
