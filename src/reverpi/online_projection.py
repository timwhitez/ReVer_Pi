"""Opt-in request-boundary observation projection; never rewrites Pi history.

A simple, task-agnostic research candidate, NOT a learned controller or a novel
replacement for SoL-Pi ObservationPack. Only fully paired tool-result text can
change. Goals, calls, reasoning and opaque Responses items stay byte-identical.
All retained handles resolve through the existing per-session archive/recovery
quota. Exposure means a completed upstream response, not proof the model read it.

The gateway serializes enabled requests per session. Prepared/failed operations
cannot be bypassed with a new op; a duplicate op must retain its exact payload.
Original and projected canonical requests are bounded private research evidence,
not a provider billing measurement. A projection is NOT a Pi compaction commit.
"""
from __future__ import annotations

import contextlib
from dataclasses import asdict, replace
import os
from pathlib import Path
import sqlite3
from typing import Any

from pydantic import Field
from .config import OnlineProjectionConfig, StrictModel
from .errors import LabError
from .memory import Archive
from .protocols import Message, validate_messages
from .util import canonical, digest, safe_id, seal_cache, unseal_cache


class ObservationMeta(StrictModel):
    call_id: str = Field(min_length=1, max_length=1024)
    tool_name: str = Field(min_length=1, max_length=1024)
    content_sha: str = Field(pattern=r"^[a-f0-9]{64}$")
    is_error: bool = Field(strict=True)  # Missing error status is conservative: the Pi client marks it True.


PROTECTED_TOOLS = frozenset({"recover_evidence", "search_evidence", "revalidate_evidence"})
MARKER = "[reverpi-observation-v1 "


def validate_observations(messages: list[Message], metadata: list[ObservationMeta] | None) -> dict[str, ObservationMeta]:
    """Require a bijection with actual tool results, not model-provided hints."""
    validate_messages(messages)
    if metadata is None:
        raise LabError("projection_metadata", "Enabled online projection requires complete observation metadata")
    names = {c["id"]: c["name"] for m in messages for c in m.calls}
    by_id: dict[str, ObservationMeta] = {}
    for item in metadata:
        if item.call_id in by_id:
            raise LabError("projection_metadata", "Duplicate observation metadata")
        by_id[item.call_id] = item
    results = [m for m in messages if m.role == "tool"]
    if set(by_id) != {m.call_id for m in results}:
        raise LabError("projection_metadata", "Observation metadata must cover exactly the tool results")
    for message in results:
        item = by_id[message.call_id]
        if item.tool_name != names[message.call_id] or item.content_sha != digest(message.content):
            raise LabError("projection_metadata", "Observation identity does not match its canonical tool result")
    return by_id


def placeholder(content: str, handle: str, tool_name: str, excerpt_bytes: int, recovery_interface: str = "legacy") -> str:
    """UTF-8-safe exact head/tail substrings; offsets in recover_evidence are codepoints."""
    if recovery_interface not in {"legacy", "split_v1"}:
        raise ValueError("Unknown recovery interface")
    guidance = ("Use recover_evidence with this handle and start/chars for exact text, or literal query to locate a span.\n"
                if recovery_interface == "legacy" else
                "Read exact historical text with recover_evidence(handle, start, chars). "
                "Locate a span with search_evidence(query, chars); use returned handle and start. "
                "Never send query to recover_evidence.\n")
    encoded = content.encode("utf-8")
    half = excerpt_bytes // 2
    head = encoded[:half].decode("utf-8", errors="ignore")
    tail = encoded[-(excerpt_bytes-half):].decode("utf-8", errors="ignore")
    info = {"handle": handle, "tool": tool_name, "original_bytes": len(encoded),
            "total_chars": len(content), "offset_unit": "unicode_codepoints"}
    return (MARKER + canonical(info) + "]\n"
            "Historical output omitted from this request, not deleted. This is not a current-state verification.\n"
            + guidance + "[head]\n" + head + "\n[middle omitted]\n" + tail + "\n[tail]")


class ProjectionStore:
    """A bounded, per-gateway SQLite trace and monotone exposure state.

    Hashes detect accidental corruption, not malicious edits by a database owner.
    This is single-operator research storage; it does not implement multi-tenant
    authorization. Actual caller authentication remains in the gateway.
    """
    def __init__(self, path: Path, config: OnlineProjectionConfig):
        self.path, self.config = Path(path).absolute(), config
        if config.mode == "off":
            raise ValueError("Disabled projection must not create a projection store")
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ValueError("Projection database must be a regular file")
        for parent in self.path.parents:
            if parent.is_symlink():
                raise ValueError("Projection database parents cannot be symlinks")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS projection_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations(
              namespace TEXT NOT NULL,call_id TEXT NOT NULL,meta_sha TEXT NOT NULL,
              full_sends INTEGER NOT NULL,packed INTEGER NOT NULL,
              PRIMARY KEY(namespace,call_id));
            CREATE TABLE IF NOT EXISTS projection_events(
              seq INTEGER PRIMARY KEY AUTOINCREMENT,namespace TEXT NOT NULL,op TEXT NOT NULL,
              payload_sha TEXT NOT NULL,state TEXT NOT NULL,reason TEXT,record TEXT NOT NULL,
              bytes INTEGER NOT NULL,UNIQUE(namespace,op));
            """)
            bound = db.execute("SELECT value FROM projection_meta WHERE key='config'").fetchone()
            value = canonical(config.model_dump())
            if bound is not None and bound[0] != value:
                raise LabError("projection_config_changed", "Projection config changed in an existing run")
            db.execute("INSERT OR IGNORE INTO projection_meta VALUES ('config',?)", (value,))
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def db(self):
        # Static-path protection, not a defense against malicious concurrent owners.
        for path in [self.path, *(Path(str(self.path) + suffix) for suffix in ("-wal", "-shm", "-journal"))]:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError("Projection SQLite file family must be regular files")
        for parent in self.path.parents:
            if parent.is_symlink():
                raise ValueError("Projection database parents cannot be symlinks")
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def prepare(self, namespace: str, op: str, payload_sha: str, messages: list[Message],
                metadata: list[ObservationMeta] | None, archive: Archive) -> dict[str, Any]:
        safe_id(namespace); safe_id(op)
        by_id = validate_observations(messages, metadata)
        # Archive verification precedes projection, including duplicate/replayed ops.
        # No overflow fallback drops observations or silently changes a method.
        archive.put_many(namespace, [m.content for m in messages if m.role == "tool"])
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM projection_events WHERE namespace=? AND op=?", (namespace, op)).fetchone()
            if previous is not None:
                if previous["payload_sha"] != payload_sha:
                    raise LabError("idempotency_conflict", "Online request op reused with different input")
                if previous["state"] == "failed":
                    raise LabError("projection_terminal", "Failed online request cannot be replayed")
                return unseal_cache(previous["record"])
            unresolved = db.execute("SELECT 1 FROM projection_events WHERE namespace=? AND state!='complete' LIMIT 1", (namespace,)).fetchone()
            if unresolved:
                raise LabError("operation_in_doubt", "Unresolved online request; do not bypass it with a new op")
            count, used = db.execute("SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM projection_events WHERE namespace=?", (namespace,)).fetchone()
            if count >= self.config.max_trace_events:
                raise LabError("projection_quota", "Projection trace event limit reached")
            result_ids = [m.call_id for m in messages if m.role == "tool"]
            recent = set(result_ids[-self.config.keep_recent_results:])
            projected: list[Message] = []
            decisions = []
            for index, message in enumerate(messages):
                if message.role != "tool":
                    projected.append(message)
                    continue
                item = by_id[message.call_id]
                identity = digest(item.model_dump())
                old = db.execute("SELECT * FROM observations WHERE namespace=? AND call_id=?", (namespace, item.call_id)).fetchone()
                if old and old["meta_sha"] != identity:
                    raise LabError("projection_identity_changed", "A prior tool result or error status changed")
                full_sends = old["full_sends"] if old else 0
                packed = bool(old["packed"]) if old else False
                size = len(message.content.encode("utf-8"))
                if item.is_error:
                    reason = "error_or_unknown_status"
                elif item.tool_name in PROTECTED_TOOLS:
                    reason = "recovery_or_current_verification"
                elif message.content.startswith(MARKER):
                    reason = "already_a_receipt"
                elif size < self.config.min_observation_bytes:
                    reason = "small_observation"
                elif not packed and item.call_id in recent:
                    reason = "recent_result"
                elif not packed and full_sends < self.config.full_exposures:
                    reason = "first_full_exposures"
                else:
                    reason = "already_projected" if packed else "aged_observation"
                eligible = reason in {"already_projected", "aged_observation"}
                receipt = placeholder(message.content, item.content_sha, item.tool_name,
                                      self.config.excerpt_bytes, self.config.recovery_interface) if eligible else None
                if receipt is not None and len(receipt.encode("utf-8")) >= size:
                    eligible = False; reason = "nonpositive_byte_saving"; receipt = None
                applied = eligible and self.config.mode == "apply"
                projected.append(replace(message, content=receipt) if applied else message)
                decisions.append({"index": index, **item.model_dump(), "full_sends_before": full_sends,
                                  "packed_before": packed, "eligible": eligible, "applied": applied,
                                  "reason": reason, "original_bytes": size,
                                  "projected_bytes": len((receipt if applied else message.content).encode("utf-8")),
                                  "meta_sha": identity})
            validate_messages(projected)
            source = [asdict(m) for m in messages]; sent = [asdict(m) for m in projected]
            record = {"schema": 1, "kind": "online_observation_projection_v1", "namespace": namespace,
                      "op": op, "mode": self.config.mode, "payload_sha": payload_sha,
                      "config": self.config.model_dump(), "source_messages": source, "sent_messages": sent,
                      "source_sha": digest(source), "sent_sha": digest(sent), "observations": decisions,
                      "eligible_count": sum(d["eligible"] for d in decisions),
                      "applied_count": sum(d["applied"] for d in decisions),
                      "source_utf8_bytes": len(canonical(source).encode()),
                      "sent_utf8_bytes": len(canonical(sent).encode()),
                      "provider_tokens_measured": False, "native_compaction_commit": False}
            encoded = seal_cache(record); size = len(encoded.encode("utf-8"))
            if used + size > self.config.max_trace_bytes:
                raise LabError("projection_quota", "Projection trace byte quota exceeded before model dispatch")
            db.execute("INSERT INTO projection_events(namespace,op,payload_sha,state,record,bytes) VALUES (?,?,?,'prepared',?,?)",
                       (namespace, op, payload_sha, encoded, size))
            return record

    def complete(self, namespace: str, op: str) -> None:
        """Exactly-once exposure commit after a completed upstream response/cache hit."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM projection_events WHERE namespace=? AND op=?", (namespace, op)).fetchone()
            if row is None or row["state"] == "failed":
                raise LabError("projection_state", "Online response has no prepared operation")
            if row["state"] == "complete":
                return
            record = unseal_cache(row["record"])
            for item in record["observations"]:
                old = db.execute("SELECT * FROM observations WHERE namespace=? AND call_id=?", (namespace, item["call_id"])).fetchone()
                if old and old["meta_sha"] != item["meta_sha"]:
                    raise LabError("projection_identity_changed", "Observation changed before exposure commit")
                sends = (old["full_sends"] if old else 0) + (0 if item["applied"] else 1)
                packed = bool(old["packed"]) if old else False
                db.execute("INSERT INTO observations VALUES (?,?,?,?,?) ON CONFLICT(namespace,call_id) DO UPDATE SET full_sends=excluded.full_sends,packed=excluded.packed",
                           (namespace, item["call_id"], item["meta_sha"], sends, int(packed or item["applied"])))
            db.execute("UPDATE projection_events SET state='complete' WHERE namespace=? AND op=?", (namespace, op))

    def fail(self, namespace: str, op: str, kind: str) -> None:
        with self.db() as db:
            db.execute("UPDATE projection_events SET state='failed',reason=? WHERE namespace=? AND op=? AND state='prepared'",
                       (kind, namespace, op))
