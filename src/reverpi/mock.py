"""Protocol-only mock, not a learned model and NEVER research performance evidence."""
from __future__ import annotations
import json
import re
import httpx
from .util import canonical


def mock_transport(provider):
    def handle(request: httpx.Request):
        body = json.loads(request.content)
        if request.url.path.endswith("/compact"):
            return httpx.Response(200, json={"id": "mock-compact", "output": [{"type": "compaction", "encrypted_content": "MOCK_ONLY"}],
                                            "usage": {"input_tokens": 20, "output_tokens": 5}})
        messages = body.get("messages", body.get("input", []))
        pieces = []
        for m in messages:
            if isinstance(m.get("content"), str):
                pieces.append(m["content"])
        last = pieces[-1] if pieces else ""
        try:
            task = json.loads(last)
        except ValueError:
            task = {}
        tools = body.get("tools", [])
        tool_names = {t.get("name", t.get("function", {}).get("name")) for t in tools}
        nonce_match = re.search(r"Nonce: ([a-f0-9]+)", "\n".join(pieces))
        tool_reply = next((m for m in reversed(messages) if m.get("role") == "tool" or m.get("type") == "function_call_output"), None)
        probe_call = "echo_nonce" in tool_names and nonce_match is not None and tool_reply is None
        if probe_call:
            text = ""
        elif "echo_nonce" in tool_names and tool_reply is not None:
            text = tool_reply.get("content", tool_reply.get("output", "{}"))
        elif task.get("task") == "compress":
            recs = task.get("records", [])
            previous = task.get("previous", "")
            text = canonical({"summary": previous + "\n" + "\n".join(r["text"] for r in recs), "citations": [r["id"] for r in recs]})
        elif task.get("task") == "diagnostic":
            memory = task.get("memory", "")
            answers = {}
            for q in task.get("questions", []):
                tag = q.get("tag") or q["id"].upper()
                found = re.findall(r"\b" + re.escape(tag) + r"=([A-Za-z0-9_.:-]+)", memory)
                answers[q["id"]] = found[-1] if found else "UNKNOWN"
            text = canonical({"answers": answers})
        elif task.get("task") == "scoped_code_review_v1":
            # A scripted transport does not perform a substantive code review.
            text = canonical({"findings": [], "verdict": "insufficient_context", "overflow": False})
        elif task.get("task") == "code_review":
            text = canonical({"findings": [], "verdict": "mock"})
        elif isinstance(task.get("questions"), list) and "memory" in task:
            # A protocol-only continuation: never read evaluator gold or pretend
            # to reason. Cover every public question with an explicit unknown.
            text = canonical({"answers": {q["id"]: "unknown" for q in task["questions"]}})
        else:
            text = '{"ok":true}'
        usage = {"prompt_tokens": max(1, len(request.content)//4), "completion_tokens": max(1, len(text)//4),
                 "prompt_tokens_details": {"cached_tokens": 0}}
        if provider.protocol == "chat_completions":
            raw = {"id": "mock-chat", "model": provider.model, "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text, "reasoning_content": "mock"}}], "usage": usage}
        else:
            raw = {"id": "mock-response", "model": provider.model, "status": "completed", "output": [{"id": "mock-message", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
                   "usage": {"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"]}}
        if probe_call:
            args = canonical({"nonce": nonce_match.group(1)})
            if provider.protocol == "chat_completions":
                raw["choices"][0]["finish_reason"] = "tool_calls"
                raw["choices"][0]["message"]["tool_calls"] = [{"id":"mock-call", "type":"function", "function":{"name":"echo_nonce","arguments":args}}]
            else:
                raw["output"] = [{"id":"mock-reason", "type":"reasoning", "encrypted_content":"MOCK_REASONING", "summary":[]},
                                 {"id":"mock-tool", "type":"function_call", "call_id":"mock-call", "name":"echo_nonce", "arguments":args}]
        return httpx.Response(200, json=raw, headers={"x-request-id": "mock-request"})
    return httpx.MockTransport(handle)
