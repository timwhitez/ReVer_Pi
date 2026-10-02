from __future__ import annotations
import asyncio
import codecs
import contextvars
import json
import math
import random
import ssl
import time
import weakref
from http.cookiejar import CookieJar, DefaultCookiePolicy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import AsyncIterator, Callable
import httpx
from .config import Provider
from .errors import LabError, classify_http
from .ledger import Ledger
from .async_ledger import AsyncLedger, Operation
from .protocols import (Completion, Message, build_request, conservative_input_tokens,
                        normalize_usage, parse_completion)
from .util import digest, strict_json_loads
from .transport_trace import exception_signature, record_transport_trace

# Trusted control-plane admission check, set by the gateway for the task that owns
# a request. It is consulted after concurrency/rate admission and before an attempt
# is reserved or sent, so queued work of a revoked session is never dispatched.
# It must raise LabError to refuse; it never comes from model or request fields.
DISPATCH_GUARD: contextvars.ContextVar[Callable[[], None] | None] = contextvars.ContextVar(
    "reverpi_dispatch_guard", default=None)


def retry_after(headers: httpx.Headers, now: datetime | None = None) -> float | None:
    try:
        if "retry-after-ms" in headers:
            value_ms = float(headers["retry-after-ms"]) / 1000
            return max(0, value_ms) if math.isfinite(value_ms) else None
        value = headers.get("retry-after")
        if value is None:
            return None
        try:
            number = float(value)
            return max(0, number) if math.isfinite(number) else None
        except ValueError:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0, (dt - (now or datetime.now(timezone.utc))).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


async def sse_events(chunks: AsyncIterator[bytes], max_bytes: int) -> AsyncIterator[dict | str]:
    # WHATWG event streams accept one leading BOM and CR, LF or CRLF lines,
    # including separators split across transport chunks.
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    buf, fields, total, previous_cr = "", [], 0, False
    async for chunk in chunks:
        total += len(chunk)
        if total > max_bytes:
            raise LabError("response_too_large", "SSE exceeded configured byte cap", ambiguous=True)
        try:
            decoded = decoder.decode(chunk)
        except UnicodeDecodeError as exc:
            raise LabError("invalid_utf8", "Invalid SSE UTF-8", ambiguous=True) from exc
        if decoded:
            if previous_cr and decoded.startswith("\n"):
                decoded = decoded[1:]
            previous_cr = decoded.endswith("\r")
            buf += decoded.replace("\r\n", "\n").replace("\r", "\n")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            if not line:
                if fields:
                    text = "\n".join(fields)
                    fields = []
                    if text == "[DONE]":
                        yield text
                    else:
                        try:
                            value = strict_json_loads(text)
                        except (ValueError, RecursionError) as exc:
                            raise LabError("malformed_sse", "Invalid JSON event", ambiguous=True) from exc
                        if not isinstance(value, dict):
                            raise LabError("malformed_sse", "Non-object JSON event", ambiguous=True)
                        yield value
            elif line.startswith("data:"):
                fields.append(line[5:].removeprefix(" "))
            # event/id/retry/comment fields are not provider output data.
    try:
        tail = decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise LabError("partial_stream", "UTF-8 ended mid-character", ambiguous=True) from exc
    if buf.strip() or fields or tail:
        raise LabError("partial_stream", "Stream ended mid-event", ambiguous=True)


async def consume_sse(p: Provider, response: httpx.Response) -> dict:
    final = None
    done = False
    chat = {"id": None, "model": None, "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": None}]}
    calls: dict[int, dict] = {}
    async for event in sse_events(response.aiter_bytes(chunk_size=65536), p.max_response_bytes):
        if done:
            raise LabError("malformed_sse", "Data after terminal marker", ambiguous=True)
        if event == "[DONE]":
            done = True
            continue
        if event.get("error") or event.get("type") == "error":
            raise LabError("stream_error", "Error event inside successful stream", ambiguous=True)
        if p.protocol == "responses":
            if final is not None:
                raise LabError("malformed_sse", "Duplicate or post-terminal Responses event", ambiguous=True)
            if event.get("type") in ("response.completed", "response.failed", "response.incomplete"):
                final = event.get("response")
                if not isinstance(final, dict):
                    raise LabError("malformed_sse", "Missing terminal response object", ambiguous=True)
                expected_status = event["type"].split(".", 1)[1]
                if final.get("status") != expected_status:
                    raise LabError("malformed_sse", "Responses event and terminal status disagree", ambiguous=True)
            continue
        for key in ("id", "model"):
            value = event.get(key)
            if value is not None:
                if not isinstance(value, str) or not value:
                    raise LabError("malformed_sse", "Stream identity must be a nonempty string", ambiguous=True)
                if chat[key] is not None and chat[key] != value:
                    kind = "model_drift" if key == "model" else "malformed_sse"
                    raise LabError(kind, "Response identity changed inside a stream", ambiguous=True)
                chat[key] = value
        if event.get("usage") is not None:
            if "usage" in chat and chat["usage"] != event["usage"]:
                raise LabError("malformed_sse", "Conflicting terminal usage reports", ambiguous=True)
            chat["usage"] = event["usage"]
        choices = event.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise LabError("malformed_sse", "Expected zero or one streaming choice", ambiguous=True)
        for choice in choices:
            if not isinstance(choice, dict):
                raise LabError("malformed_sse", "Stream choice must be an object", ambiguous=True)
            index = choice.get("index", 0)
            if type(index) is not int or index != 0:
                raise LabError("multiple_choices", "n>1 is not supported")
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise LabError("malformed_sse", "Stream delta must be an object", ambiguous=True)
            if "role" in delta and delta["role"] != "assistant":
                raise LabError("malformed_sse", "Unexpected stream role", ambiguous=True)
            if chat["choices"][0]["finish_reason"] is not None:
                raise LabError("malformed_sse", "Choice data after a terminal Chat finish; only usage-only events are allowed", ambiguous=True)
            m = chat["choices"][0]["message"]
            for key in ("content", "reasoning_content"):
                if delta.get(key) is not None:
                    if not isinstance(delta[key], str):
                        raise LabError("malformed_sse", "Text delta must be a string", ambiguous=True)
                    m[key] = m.get(key, "") + delta[key]
            if delta.get("refusal"):
                m["refusal"] = delta["refusal"]
            fragments = delta.get("tool_calls", [])
            if fragments is None:
                fragments = []
            if not isinstance(fragments, list):
                raise LabError("malformed_sse", "Tool deltas must be an array", ambiguous=True)
            for c in fragments:
                if not isinstance(c, dict) or ("type" in c and c["type"] != "function"):
                    raise LabError("malformed_sse", "Unsupported tool delta", ambiguous=True)
                i = c.get("index")
                if type(i) is not int or i < 0:
                    raise LabError("malformed_sse", "Missing tool call index")
                target = calls.setdefault(i, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if c.get("id") is not None:
                    if not isinstance(c["id"], str) or not c["id"] or (target["id"] and target["id"] != c["id"]):
                        raise LabError("malformed_sse", "Invalid or changing tool call identity", ambiguous=True)
                    target["id"] = c["id"]
                function = c.get("function", {})
                if not isinstance(function, dict):
                    raise LabError("malformed_sse", "Tool function fragment must be an object", ambiguous=True)
                for k in ("name", "arguments"):
                    value = function.get(k)
                    if value is not None:
                        if not isinstance(value, str):
                            raise LabError("malformed_sse", "Function fragments must be strings", ambiguous=True)
                        target["function"][k] += value
            if choice.get("finish_reason") is not None:
                chat["choices"][0]["finish_reason"] = choice["finish_reason"]
    if p.protocol == "responses":
        if final is None:
            raise LabError("partial_stream", "No Responses terminal event", ambiguous=True)
        return final
    if not done or chat["choices"][0]["finish_reason"] is None:
        raise LabError("partial_stream", "No complete Chat terminal sequence", ambiguous=True)
    if calls:
        if list(sorted(calls)) != list(range(len(calls))):
            raise LabError("malformed_sse", "Missing tool call fragment index")
        chat["choices"][0]["message"]["tool_calls"] = [calls[i] for i in sorted(calls)]
    return chat


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


class APIClient:
    """No SDK retry layer. A single retry policy owns all attempts and billing."""
    def __init__(self, provider: Provider, ledger: Ledger, *, transport=None,
                 sleeper: Callable = asyncio.sleep, random_source=None,
                 ledger_max_pending: int = 32, ledger_cleanup_seconds: float = 2):
        self.p, self.ledger = provider, ledger
        self.sleeper = sleeper
        self.rng = random_source or random.Random()
        self.sem = asyncio.Semaphore(provider.concurrency)
        self.dispatch_lock = asyncio.Lock()
        self.binding_lock = asyncio.Lock()
        self.provider_bound = False
        self.op_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self.async_ledger = AsyncLedger(ledger, max_pending=ledger_max_pending,
                                        cleanup_seconds=ledger_cleanup_seconds)
        self.ledger_owner = contextvars.ContextVar("request_ledger_owner")
        self.requests: set[asyncio.Task] = set()
        self.closing = False
        self.close_task = None
        verify: bool | ssl.SSLContext = True
        if provider.ca_bundle_env:
            import os
            path = os.environ.get(provider.ca_bundle_env)
            if not path:
                raise ValueError("Configured CA bundle environment variable is missing")
            verify = ssl.create_default_context(cafile=path)
        if provider.mock and transport is None:
            from .mock import mock_transport
            transport = mock_transport(provider)
        self.http = httpx.AsyncClient(
            transport=transport, verify=verify, trust_env=False, follow_redirects=False,
            cookies=CookieJar(policy=_NoCookies()),
            timeout=httpx.Timeout(connect=provider.connect_seconds, read=provider.read_seconds,
                                  write=provider.write_seconds, pool=provider.pool_seconds),
            limits=httpx.Limits(max_connections=provider.concurrency, max_keepalive_connections=provider.concurrency))
        self.fingerprint = digest(provider.model_dump())
        b = ledger.budget
        if (b.max_total_usd or b.per_cell_usd) and not provider.prices.configured:
            raise ValueError("Currency limits require a configured frozen price table")

    async def close(self):
        if self.close_task is None:
            self.closing = True
            self.close_task = asyncio.create_task(self._close())
        await asyncio.shield(self.close_task)

    async def _close(self):
        for task in tuple(self.requests):
            task.cancel()
        await asyncio.gather(*tuple(self.requests), return_exceptions=True)
        await self.async_ledger.close()
        await self.http.aclose()

    async def _ledger_call(self, method, *args, cleanup=False, **kwargs):
        return await self.async_ledger.call(self.ledger_owner.get(), method, *args,
                                             cleanup=cleanup, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def complete(self, messages: list[Message], *, op: str, cell: str,
                       tools=None, effort=None, max_output_tokens=None) -> Completion:
        path, body = build_request(self.p, messages, tools, effort=effort, max_output_tokens=max_output_tokens)
        result = await self.request(path, body, op=op, cell=cell)
        return Completion(**result)

    async def native_compact(self, items: list[dict], *, op: str, cell: str) -> dict:
        if self.p.protocol != "responses" or not self.p.supports_native_compaction:
            raise LabError("capability", "Native opaque compaction must be explicitly enabled on a Responses profile")
        # The standalone endpoint exposes no documented output-token ceiling.
        # This is an observational optional track, NEVER advertised as hard-capped.
        body = {"model": self.p.model, "input": items}
        return await self.request("/responses/compact", body, op=op, cell=cell, native=True)

    async def request(self, path: str, body: dict, *, op: str, cell: str, native=False) -> dict:
        from .preflight import validate_provider_environment
        validate_provider_environment(self.p)
        payload_sha = digest({"profile": self.fingerprint, "path": path, "body": body})
        if self.closing:
            raise LabError("client_closed", "API client is closing")
        owner = Operation(time.monotonic() + self.p.retry.total_seconds)
        task = asyncio.current_task()
        initial_cancellations = task.cancelling()
        self.requests.add(task)
        token = self.ledger_owner.set(owner)
        lock = self.op_locks.setdefault(op, asyncio.Lock())
        acquired = False
        try:
            async with asyncio.timeout_at(owner.deadline):
                await lock.acquire()
                acquired = True
                async with self.binding_lock:
                    if not self.provider_bound:
                        await self._ledger_call("bind", "provider:" + self.p.name, self.p.model_dump())
                        self.provider_bound = True
                cached = await self._ledger_call("claim", op, payload_sha, cell)
                if cached is not None:
                    return cached
                result = await self._execute(path, body, op, cell, native)
                owner.check()
            owner.cleanup_deadline = owner.cleanup_deadline or time.monotonic() + self.async_ledger.cleanup_seconds
            await self._ledger_call("finish", op, result=result, cleanup=True)
            if owner.cancelled:
                raise asyncio.CancelledError
            return result
        except BaseException as exc:
            interruptions = owner.interruptions
            # A bounded cleanup waiter can defer caller cancellation and then
            # expire with TimeoutError. Preserve external cancellation; the
            # execution timeout context removes its own cancellation count.
            caller_cancelled = isinstance(exc, asyncio.CancelledError) or task.cancelling() > initial_cancellations
            owner.cancel()
            if caller_cancelled:
                err = LabError("cancelled", "Operation cancelled; possible dispatched attempts remain reserved", ambiguous=True)
            elif isinstance(exc, TimeoutError):
                err = LabError("total_timeout", "Total operation deadline exceeded", ambiguous=True)
            elif isinstance(exc, LabError):
                err = exc
            elif isinstance(exc, Exception):
                err = LabError("internal_error", f"Internal {type(exc).__name__}; no automatic resend", ambiguous=True)
            else:
                raise
            try:
                await self.async_ledger.drain(owner)
                # A claim/reservation may have COMMITted just as its waiter was
                # cancelled. Accept its own real result before any further write.
                def succeeded(job):
                    return job.future.done() and job.future.exception() is None
                claimed = any(j.fn == self.ledger.claim and succeeded(j) and j.future.result() is None for j in owner.jobs)
                finished = any(j.fn == self.ledger.finish and succeeded(j) for j in owner.jobs)
                settled = {j.args[0] for j in owner.jobs if j.fn == self.ledger.settle and succeeded(j)}
                for job in tuple(owner.jobs):
                    if job.fn == self.ledger.reserve_gated and succeeded(job):
                        aid, _ = job.future.result()
                        if aid is not None and aid not in settled:
                            await self._ledger_call("settle", aid, tokens=None, usd=None,
                                                   error_kind=err.kind, cleanup=True)
                if claimed and not finished:
                    await self._ledger_call("finish", op, error=err, cleanup=True)
            except (TimeoutError, LabError) as cleanup_error:
                # No replay/parallel finish: durable running/reserved rows are the
                # existing recovery contract when bounded reconciliation fails.
                if caller_cancelled or owner.interruptions > interruptions:
                    raise asyncio.CancelledError from cleanup_error
                raise LabError("ledger_cleanup_timeout", "Ledger cleanup deadline exceeded; outcome is in doubt to this caller and unresolved reservations remain charged", ambiguous=True) from cleanup_error
            if owner.interruptions > interruptions:
                raise asyncio.CancelledError
            if caller_cancelled:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise asyncio.CancelledError from exc
            if err is exc:
                raise
            raise err from exc
        finally:
            if acquired:
                lock.release()
            self.ledger_owner.reset(token)
            self.requests.discard(task)

    async def _execute(self, path, body, op, cell, native):
        p = self.p
        inp = conservative_input_tokens(body)
        out = p.native_compact_output_reservation if native else body.get("max_output_tokens", body.get(p.chat_output_field, p.max_output_tokens))
        if p.context_admission == "byte_bound" and inp + out + p.context_margin_tokens > p.context_window:
            raise LabError("context_overflow", "Conservative full serialized request exceeds context reservation")
        reserve_tokens = inp + out
        cost = (inp * max(p.prices.input_per_million, p.prices.cached_input_per_million)
                + out * p.prices.output_per_million) / 1e6
        headers = {"Authorization": "Bearer " + p.secret(), "Content-Type": "application/json", "Accept": "text/event-stream" if p.stream and not native else "application/json"}
        import os
        for k, env in p.extra_headers_env.items():
            if env not in os.environ or "\n" in os.environ[env] or "\r" in os.environ[env]:
                raise LabError("configuration", "Missing/invalid extra header environment variable")
            headers[k] = os.environ[env]
        last = LabError("unreachable", "No attempt executed")
        guard = DISPATCH_GUARD.get()
        earlier_ambiguous = False  # Any previous attempt of this op may have been processed upstream.
        for attempt_no in range(p.retry.max_attempts):
            async with self.sem:
                while True:
                    if guard is not None:
                        try:
                            guard()  # Refusal creates no NEW attempt; earlier attempts keep their records.
                        except LabError as refused:
                            if attempt_no == 0:
                                raise
                            raise LabError(refused.kind, "Refused before a retry; earlier attempts of this "
                                           "operation keep their recorded (possibly unknown) usage",
                                           ambiguous=earlier_ambiguous, status=refused.status) from refused
                    async with self.dispatch_lock:
                        aid, delay = await self._ledger_call("reserve_gated",
                            op, cell, p.name, reserve_tokens, cost,
                            p.requests_per_minute, p.tokens_per_minute)
                        # Worker/SQLite admission added an await. Recheck the
                        # original trusted guard before this async owner sends.
                        if guard is not None:
                            try:
                                guard()
                            except LabError as refused:
                                if aid is not None:
                                    await self._ledger_call("settle", aid, tokens=0,
                                        usd=0 if p.prices.configured else None,
                                        error_kind="not_dispatched", cleanup=True)
                                self.ledger_owner.get().check()
                                if attempt_no:
                                    raise LabError(refused.kind, "Refused before a retry; earlier attempts keep their recorded usage",
                                                   ambiguous=earlier_ambiguous, status=refused.status) from refused
                                raise
                        if aid is not None:
                            break
                    await self.sleeper(delay)
                settled = False
                last = LabError("attempt_interrupted", "Attempt interrupted before a conclusive response", ambiguous=True)
                status, request_id, raw, retry_hint = None, None, None, None
                attempt_started = time.monotonic()
                phase, completed, transport_exception = "await_headers", False, None
                try:
                    async with self.http.stream("POST", p.base_url.rstrip("/") + path, json=body, headers=headers) as resp:
                        status = resp.status_code
                        phase = "read_body"
                        request_id = resp.headers.get("x-request-id", resp.headers.get("request-id"))
                        retry_hint = retry_after(resp.headers)
                        if not 200 <= status < 300:
                            b = bytearray()
                            async for part in resp.aiter_bytes(chunk_size=65536):
                                b.extend(part)
                                if len(b) > p.max_response_bytes:
                                    break
                            try:
                                error_body = strict_json_loads(b)
                            except (ValueError, UnicodeError, RecursionError):
                                error_body = {}
                            if isinstance(error_body, dict) and error_body.get("usage") is not None:
                                # An explicit but invalid usage claim is UNKNOWN, not a zero-cost rejection.
                                error_tokens, error_usd = normalize_usage(error_body["usage"], p.prices)
                                await self._ledger_call("settle",aid, tokens=error_tokens, usd=error_usd, raw_usage=error_body["usage"], request_id=request_id, status=status, cleanup=True)
                                settled = True
                            raise classify_http(status, error_body)
                        if "text/event-stream" in resp.headers.get("content-type", "").lower():
                            if native:
                                raise LabError("unsupported_stream", "Standalone compaction requires a complete JSON response", ambiguous=True)
                            raw = await consume_sse(p, resp)
                        else:
                            b = bytearray()
                            async for part in resp.aiter_bytes(chunk_size=65536):
                                b.extend(part)
                                if len(b) > p.max_response_bytes:
                                    raise LabError("response_too_large", "Response exceeded cap", ambiguous=True)
                            try:
                                raw = strict_json_loads(b)
                            except (ValueError, UnicodeError, RecursionError) as exc:
                                raise LabError("malformed_json", "Complete HTTP body was not valid JSON", ambiguous=True) from exc
                    phase = "validate_response"
                    if not isinstance(raw, dict):
                        raise LabError("malformed_response", "Expected JSON object", ambiguous=True)
                    tokens, usd = normalize_usage(raw.get("usage"), p.prices)
                    await self._ledger_call("settle",aid, tokens=tokens, usd=usd, raw_usage=raw.get("usage"), request_id=request_id, status=status, cleanup=True)
                    settled = True
                    if native:
                        self.ledger_owner.get().check()
                        if not isinstance(raw.get("output"), list) or not raw["output"]:
                            raise LabError("empty_compaction", "Native compaction returned no window")
                        completed = True
                        return raw
                    result = parse_completion(p, raw)
                    # A received response retains its identity evidence through
                    # cancellation during settlement, as well as its known usage.
                    await self._ledger_call("observed_model", p.name, result.model, p.expected_response_model,
                                            stop_on_drift=True, cleanup=True)
                    self.ledger_owner.get().check()
                    completed = True
                    return result.to_dict()
                except httpx.ConnectError as exc:
                    transport_exception = exception_signature(exc)
                    cause = exc
                    tls_error = False
                    while cause is not None:
                        if isinstance(cause, ssl.SSLCertVerificationError):
                            tls_error = True
                            break
                        cause = cause.__cause__
                    last = (LabError("tls_configuration", "TLS certificate verification failed; verification is never disabled", False, False)
                            if tls_error else LabError("connect_error", "Connection could not be established", True, False))
                except (httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                    transport_exception = exception_signature(exc)
                    last = LabError("connect_timeout", "Connection/pool timeout before response", True, False)
                except (httpx.ReadError, httpx.WriteError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
                    transport_exception = exception_signature(exc)
                    last = LabError("transport_ambiguous", "Request or response interrupted; upstream may have processed it", True, True)
                except LabError as err:
                    last = err
                finally:
                    import sys
                    active_exception = sys.exc_info()[1]
                    owner = self.ledger_owner.get()
                    if active_exception is not None or owner.cancelled or time.monotonic() >= owner.deadline:
                        owner.cleanup_deadline = owner.cleanup_deadline or time.monotonic() + self.async_ledger.cleanup_seconds
                    if transport_exception is None and active_exception is not None:
                        transport_exception = exception_signature(active_exception)
                    await self._ledger_call(record_transport_trace, self.ledger, attempt_id=aid, phase=phase,
                        elapsed_seconds=time.monotonic()-attempt_started, status=status,
                        complete=completed, exception=transport_exception,
                        timeout_config={"read":p.read_seconds,"write":p.write_seconds,
                            "connect":p.connect_seconds,"pool":p.pool_seconds,
                            "total":p.retry.total_seconds}, cleanup=True)
                    if not settled:
                        # Only explicit admission rejection / pre-send failures get zero.
                        safe_zero = last.kind in {"tls_configuration", "connect_error", "connect_timeout", "authentication", "permission", "quota", "invalid_request", "endpoint_or_model", "rate_limit", "context_overflow", "redirect_rejected"}
                        await self._ledger_call("settle",aid, tokens=0 if safe_zero else None,
                                           usd=0 if safe_zero and p.prices.configured else None,
                                           request_id=request_id, status=status, error_kind=last.kind, cleanup=True)
            self.ledger_owner.get().check()
            earlier_ambiguous = earlier_ambiguous or last.ambiguous
            if last.retryable:
                jitter = self.rng.uniform(0, min(p.retry.cap_seconds, p.retry.base_seconds * 2**attempt_no))
                delay = max(jitter, retry_hint or 0)
                await self._ledger_call("cooldown",p.name, delay, failure=last.kind != "rate_limit",
                                     threshold=p.retry.circuit_failures, circuit_seconds=p.retry.circuit_cooldown_seconds)
                if retry_hint is not None and retry_hint > p.retry.max_retry_after_seconds:
                    raise LabError("provider_cooldown", "Retry-After exceeds permitted wait; request not hammered", status=status)
            if not last.retryable or last.ambiguous and not p.retry.retry_ambiguous:
                raise last
            if attempt_no + 1 >= p.retry.max_attempts:
                raise last
            self.ledger_owner.get().check()
            # A fully reconciled safe retry starts a new attempt's accounting
            # allowance, while its ordinary work retains the original deadline.
            self.ledger_owner.get().cleanup_deadline = None
            # Delay is owned by the shared gate, including the final failed attempt.
        raise last
