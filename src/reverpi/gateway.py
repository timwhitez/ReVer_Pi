"""Loopback/private-network gateway. One worker, one immutable profile per run.

A session bearer grants ONLY its own cell. It is not an upstream API key. The
actor cannot select another cell or get the ledger/admin API. Run the gateway
outside the task sandbox; expose it only on a restricted network.
"""
from __future__ import annotations
import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import weakref
from pathlib import Path
from typing import Any
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError
from pydantic import Field
from .config import Provider, StudyConfig, StrictModel
from .errors import LabError
from .ledger import Ledger
from .memory import Archive, Compressor, Memory, Record, METHODS
from .protocols import Message, validate_messages
from .transport import APIClient, DISPATCH_GUARD
from .online_projection import ObservationMeta, ProjectionStore
from .util import canonical, digest, safe_id, source_manifest, strict_json_loads, seal_cache, unseal_cache


class Sessions:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS sessions(
              id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, method TEXT NOT NULL,
              created REAL NOT NULL, expires REAL NOT NULL, disabled INTEGER NOT NULL,
              revision INTEGER NOT NULL, memory TEXT, metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS compact_ops(
              session TEXT, op TEXT, sha TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
              PRIMARY KEY(session,op));
            ''')
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def create(self, session: str, method: str, *, ttl: int = 86400, metadata: dict | None = None) -> str:
        safe_id(session)
        if method not in METHODS and method not in {"pi_native", "pi_original"}:
            raise ValueError("Unknown method")
        if not 60 <= ttl <= 604800:
            raise ValueError("Session TTL must be between one minute and seven days")
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,0,0,NULL,?)",
                       (session, hashlib.sha256(token.encode()).hexdigest(), method, time.time(), time.time()+ttl,
                        canonical(metadata or {})))
        return token

    def auth(self, token: str) -> dict:
        if not token or len(token) > 512:
            raise LabError("gateway_auth", "Invalid or expired session bearer", status=401)
        hashed = hashlib.sha256(token.encode()).hexdigest()
        with self.db() as db:
            r = db.execute("SELECT * FROM sessions WHERE token_hash=?", (hashed,)).fetchone()
        if not r or not hmac.compare_digest(hashed, r["token_hash"]) or r["disabled"] or r["expires"] <= time.time():
            raise LabError("gateway_auth", "Invalid or expired session bearer", status=401)
        return self._decoded(r)

    @staticmethod
    def _decoded(row):
        result = dict(row)
        if result["memory"] is not None:
            result["memory"] = canonical(unseal_cache(result["memory"]))
        return result

    def get(self, session: str) -> dict:
        with self.db() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session,)).fetchone()
            if row is None:
                raise LabError("session_missing", "Unknown session")
            return self._decoded(row)

    def disable(self, session: str):
        with self.db() as db:
            db.execute("UPDATE sessions SET disabled=1 WHERE id=?", (session,))

    def claim_compaction(self, session: str, op: str, sha: str, revision: int):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM compact_ops WHERE session=? AND op=?", (session, op)).fetchone()
            if old:
                if old["sha"] != sha:
                    raise LabError("idempotency_conflict", "Compaction ID/payload mismatch")
                if old["state"] != "complete":
                    raise LabError("operation_in_doubt", "Compaction interrupted or rejected; not silently replayed")
                return unseal_cache(old["result"])
            state = db.execute("SELECT revision FROM sessions WHERE id=?", (session,)).fetchone()
            if not state or state[0] != revision:
                raise LabError("stale_revision", "Compaction based on a stale memory revision")
            db.execute("INSERT INTO compact_ops VALUES (?,?,?,'running',NULL)", (session, op, sha))
        return None

    def commit(self, session: str, op: str, revision: int, memory: Memory):
        result = {"revision": revision+1, "memory": memory.model_dump()}
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE sessions SET revision=?,memory=? WHERE id=? AND revision=? AND disabled=0 AND expires>strftime('%s','now')",
                                 (revision+1, seal_cache(memory.model_dump()), session, revision)).rowcount
            if changed != 1:
                raise LabError("stale_revision", "Memory changed before commit")
            changed = db.execute("UPDATE compact_ops SET state='complete',result=? WHERE session=? AND op=? AND state='running'",
                                 (seal_cache(result), session, op)).rowcount
            if changed != 1:
                raise LabError("ledger_state", "Compaction not running")
        return result

    def failed(self, session: str, op: str):
        with self.db() as db:
            db.execute("UPDATE compact_ops SET state='failed' WHERE session=? AND op=? AND state='running'", (session, op))


class CompleteBody(StrictModel):
    op: str
    observation_meta: list[ObservationMeta] | None = Field(None, max_length=20000)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=20000)
    tools: list[dict[str, Any]] | None = None
    effort: str | None = None
    max_output_tokens: int | None = None


class CompactBody(StrictModel):
    op: str
    expected_revision: int = Field(ge=0)
    records: list[Record] = Field(min_length=1, max_length=20000)
    active_query: str = Field("", max_length=100000)


class RecoverBody(StrictModel):
    op: str
    handle: str | None = None
    query: str | None = None
    start: int = 0
    chars: int = 2000


def create_app(provider: Provider, study: StudyConfig, run: Path, *, transport=None,
               completion_backend=None) -> FastAPI:
    """Create the private gateway.

    completion_backend is a trusted in-process experimental dependency, never a
    request parameter. Default production dispatch and accounting are unchanged.
    A backend owns its separate shared accounting and must expose async aclose().
    """
    if set(study.methods) - (set(METHODS) | {"pi_original", "pi_native"}):
        raise ValueError("Unknown gateway compression method")
    if study.online_projection.mode != "off" and not provider.mock:
        if provider.model not in {"deepseek-flash", "gpt-6-luna"} or provider.effort != "low" or provider.concurrency != 1:
            raise LabError("online_profile", "Live research projection requires an approved model / low / concurrency 1")
    run = Path(run)
    run.mkdir(parents=True, exist_ok=True, mode=0o700)
    ledger = Ledger(run / "ledger.sqlite", study.budget)
    ledger.bind("gateway_study", study.model_dump())
    ledger.bind("gateway_code", digest(source_manifest(Path(__file__).resolve().parents[2])))
    sessions = Sessions(run / "sessions.sqlite")
    archive = Archive(run / "archive.sqlite", study.compression)
    client = APIClient(provider, ledger, transport=transport)
    projection = (ProjectionStore(run / "projection.sqlite", study.online_projection)
                  if study.online_projection.mode != "off" else None)
    online_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
    locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            try:
                await client.close()
            finally:
                if completion_backend is not None:
                    await completion_backend.aclose()

    app = FastAPI(title="ReVer-Pi private gateway", version="0.1.3rc4", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.client, app.state.sessions, app.state.ledger = client, sessions, ledger
    app.state.projection, app.state.archive = projection, archive

    @app.exception_handler(LabError)
    async def lab_error(request: Request, err: LabError):
        status = 401 if err.kind == "gateway_auth" else 409 if err.kind in {"stale_revision", "idempotency_conflict", "operation_in_doubt"} else 422
        return JSONResponse({"error": err.record()}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, err: RequestValidationError):
        return JSONResponse({"error": {"kind": "gateway_schema", "message": "Invalid request schema; payload not echoed"}}, status_code=422)

    @app.exception_handler(ValueError)
    async def value_error(request: Request, err: ValueError):
        return JSONResponse({"error": {"kind": "gateway_schema", "message": "Invalid request field"}}, status_code=422)

    @app.middleware("http")
    async def bounded_body(request, call_next):
        # Content-Length is not trusted; enforce actual streamed bytes as well.
        limit = provider.max_response_bytes
        data = bytearray()
        try:
            async with asyncio.timeout(study.gateway_body_seconds):
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data) > limit:
                        return JSONResponse({"error": {"kind": "gateway_body_too_large"}}, status_code=413)
        except TimeoutError:
            return JSONResponse({"error": {"kind": "gateway_body_timeout"}}, status_code=408)
        # FastAPI also parses bodies lacking Content-Type as JSON. Validate all
        # supported JSON media types before its permissive last-key-wins decoder.
        media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if data and (not media or media == "application/json" or media.endswith("+json")):
            try:
                strict_json_loads(data)
            except (ValueError, RecursionError):
                return JSONResponse({"error": {"kind": "gateway_schema", "message": "Invalid or ambiguous JSON; payload not echoed"}}, status_code=422)
        request._body = bytes(data)
        return await call_next(request)

    def authenticate(request):
        value = request.headers.get("authorization", "")
        if not value.startswith("Bearer "):
            raise LabError("gateway_auth", "A scoped bearer is required")
        state = sessions.auth(value[7:])
        if state["method"] not in study.methods:
            raise LabError("method_not_in_plan", "Session method was not declared in this gateway's frozen configuration")
        return state

    def require_live(session_id: str):
        """Pre-dispatch check: a revoked or expired session gets no new upstream attempt."""
        state = sessions.get(session_id)
        if state["disabled"] or state["expires"] <= time.time():
            raise LabError("session_revoked", "Session was revoked before dispatch; no upstream request was sent")

    async def guarded(work, request: Request, session_id: str):
        """Revoke/disconnect cancels upstream work; unknown billing remains reserved.

        Queued work re-checks the session after concurrency/rate admission (and on
        every retry) via DISPATCH_GUARD, which the task inherits from this context.
        """
        token = DISPATCH_GUARD.set(lambda: require_live(session_id))
        try:
            task = asyncio.create_task(work)
        finally:
            DISPATCH_GUARD.reset(token)
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=.25)
                if done:
                    break
                state = sessions.get(session_id)
                if state["disabled"] or state["expires"] <= time.time():
                    raise LabError("session_revoked", "Session was revoked while the request was executing", ambiguous=True)
                if await request.is_disconnected():
                    raise LabError("client_disconnected", "Client disconnected; possible dispatched work remains reserved", ambiguous=True)
            result = await task
            # Completion can win the polling race; revocation still wins delivery.
            state = sessions.get(session_id)
            if state["disabled"] or state["expires"] <= time.time():
                raise LabError("session_revoked", "Session was revoked before response delivery", ambiguous=True)
            return result
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def respond(work_factory):
        """Early headers and optional JSON whitespace for /complete AND /compact.

        This is a transport intervention, not a guarantee about every proxy.
        HTTP status cannot change after headers: the final body is authoritative.
        Disconnect cancellation is propagated, with ambiguous billing left intact.
        """
        if not study.early_response_headers:
            return await work_factory()

        async def body_stream():
            work = asyncio.create_task(work_factory())
            try:
                while not work.done():
                    interval = study.response_heartbeat_seconds
                    if interval == 0:
                        await asyncio.wait({work})
                    else:
                        done, _ = await asyncio.wait({work}, timeout=interval)
                        if not done:
                            yield b" "
                yield canonical(await work).encode("utf-8")
            except LabError as err:
                yield canonical({"error": err.record()}).encode("utf-8")
            except Exception:
                # Do not leak request text, credentials, stack traces, or falsely
                # treat an unhandled late error as an empty successful response.
                yield canonical({"error": {"kind": "gateway_internal", "message":
                    "Late gateway failure; inspect private diagnostics; no automatic retry"}}).encode("utf-8")
            finally:
                if not work.done():
                    work.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await work

        return StreamingResponse(body_stream(), media_type="application/json",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/health")
    async def health():
        return {"ok": True, "schema": 1}  # No model/key/run contents without authentication.

    @app.get("/session")
    async def session_info(request: Request):
        state = authenticate(request)
        return {"id": state["id"], "method": state["method"], "revision": state["revision"],
                "online_projection": {**study.online_projection.model_dump(),
                    "mode": "off" if state["method"] == "pi_original" else study.online_projection.mode,
                    "kind": "request_boundary_observations_not_native_compaction"},
                "model": provider.model, "effort": provider.effort,
                "effort_mapping": provider.effort_map[provider.effort],
                "reasoning_style": provider.reasoning_style, "protocol": provider.protocol,
                "context_window": provider.context_window, "max_output_tokens": provider.max_output_tokens,
                "request_deadline_ms": int((provider.retry.total_seconds + 30)*1000),
                "prices": provider.prices.model_dump(), "memory": json.loads(state["memory"]) if state["memory"] else None,
                "accounting": ledger.totals(state["id"]), "mock": provider.mock,
                "recovery": {**({"interface": study.online_projection.recovery_interface}
                               if study.online_projection.recovery_interface != "legacy" else {}),
                             "max_chars": study.compression.recovery_chars,
                             "max_calls": study.compression.recovery_calls,
                             **({"search_mode": study.compression.recovery_search_mode}
                                if study.compression.recovery_search_mode != "head" else {})}}

    @app.post("/complete")
    async def complete(body: CompleteBody, request: Request):
        state = authenticate(request)
        safe_id(body.op)
        if body.effort is not None and body.effort != provider.effort:
            raise LabError("effort_changed", "This run's actor effort is frozen; select a different profile/run")
        try:
            messages = [Message.from_dict(m) for m in body.messages]
            validate_messages(messages)
        except (TypeError, ValueError) as exc:
            raise LabError("message_schema", "Invalid canonical message") from exc
        enabled = projection is not None and state["method"] != "pi_original"
        async def execute_request():
            prepared = None
            try:
                sent = messages
                if enabled:
                    prepared = projection.prepare(state["id"], body.op, digest(body.model_dump()),
                                                  messages, body.observation_meta, archive)
                    sent = [Message.from_dict(m) for m in prepared["sent_messages"]]
                if state["method"] != "pi_original":
                    # One batch; tool results already verified by prepare are not re-checked.
                    verified = {m.content for m in messages if m.role == "tool"} if prepared is not None else set()
                    archive.put_many(state["id"], [m.content for m in messages
                                                   if m.content and m.content not in verified])
                # Pairing can stop BEFORE any model claim/reservation. It never
                # edits counters or turns an unresolved upstream call into a replay.
                work = (completion_backend(state=state, body=body, sent=sent, prepared=prepared)
                        if completion_backend is not None else
                        client.complete(sent, tools=body.tools, effort=body.effort,
                                        max_output_tokens=body.max_output_tokens,
                                        op="actor:"+state["id"]+":"+body.op, cell=state["id"]))
                result = await guarded(work, request, state["id"])
                if prepared is not None:
                    projection.complete(state["id"], body.op)
                return result.to_dict()
            except BaseException as error:
                if prepared is not None:
                    # Preserve the original exception if tracing itself fails.
                    with contextlib.suppress(Exception):
                        projection.fail(state["id"], body.op, error.kind if isinstance(error, LabError) else type(error).__name__)
                raise
        async def work():
            if not enabled:
                return await execute_request()
            lock = online_locks.setdefault(state["id"], asyncio.Lock())
            async with lock:
                return await execute_request()
        return await respond(work)

    @app.post("/compact")
    async def compact(body: CompactBody, request: Request):
        state = authenticate(request)
        safe_id(body.op)
        if state["method"] in {"pi_native", "pi_original"} or study.online_projection.mode != "off":
            raise LabError("native_boundary", "Native Pi owns full-history compaction in this condition")
        async def work():
            lock = locks.setdefault(state["id"], asyncio.Lock())
            async with lock:
                cached = sessions.claim_compaction(state["id"], body.op, digest(body.model_dump()), body.expected_revision)
                if cached is not None:
                    return cached
                try:
                    current = sessions.get(state["id"])
                    previous = Memory.model_validate_json(current["memory"]) if current["memory"] else None
                    memory = await guarded(Compressor(study.compression, archive, client).compress(
                        body.records, state["method"], cell=state["id"], previous=previous, active_query=body.active_query), request, state["id"])
                    return sessions.commit(state["id"], body.op, body.expected_revision, memory)
                except BaseException:
                    sessions.failed(state["id"], body.op)
                    raise
        return await respond(work)

    @app.post("/recover")
    async def recover(body: RecoverBody, request: Request):
        state = authenticate(request)
        safe_id(body.op)
        return archive.recover(state["id"], body.op, **body.model_dump(exclude={"op"}, exclude_none=True))

    return app
