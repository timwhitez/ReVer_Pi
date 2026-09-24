"""Development candidate: complete visible evidence directory, no silent omissions.

This is an ablation candidate, not a certified improvement. A directory that
cannot fit raises before replacing memory; this module never invents extra
context capacity or silently switches to another experimental condition.
"""
from __future__ import annotations
from typing import Literal
from pydantic import Field
from .config import StrictModel
from .errors import LabError
from .memory import Record, Memory, _validity
from .util import canonical, digest


class Card(StrictModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    kind: Literal["goal", "constraint", "observation", "action", "edit", "verification", "obligation", "resolution", "note"]
    handle: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependencies: dict[str, str] = Field(default_factory=dict)
    body: str | None = None


async def compact_indexed(compressor, records: list[Record], *, cell: str,
                          previous: Memory | None = None, active_query: str = "") -> Memory:
    cfg = compressor.cfg
    method = "rever_indexed"
    if not records and previous is None:
        raise LabError("empty_history", "Nothing to compact")
    if len({r.id for r in records}) != len(records):
        raise LabError("duplicate_record", "Record identifiers must be unique")
    if previous is not None and previous.method != method:
        raise LabError("method_changed", "Recursive memory cannot silently switch algorithms")
    old = previous.details if previous else {}
    if previous and old.get("indexed_config_sha") != digest(cfg.model_dump()):
        raise LabError("identity_changed", "Indexed compression configuration changed")
    if previous and "indexed_cards" not in old:
        raise LabError("memory_corruption", "Missing persistent evidence directory")
    try:
        cards = [Card.model_validate(x) for x in old.get("indexed_cards", [])]
    except (ValueError, TypeError) as exc:
        raise LabError("memory_corruption", "Invalid persistent evidence directory") from exc
    hashes = dict(old.get("record_hashes", {}))
    if len({c.id for c in cards}) != len(cards) or set(hashes) != {c.id for c in cards}:
        raise LabError("memory_corruption", "Directory and identity registry disagree")
    versions = dict(old.get("current_versions", {}))
    new_records = []
    for r in records:
        sha = digest(r.model_dump())
        if r.id in hashes:
            if hashes[r.id] != sha:
                raise LabError("record_identity_changed", "Record ID reused with changed evidence")
            continue  # An exact replay must not roll the version state backwards.
        hashes[r.id] = sha
        versions.update(r.updates)
        handle = compressor.archive.put(cell, r.text)
        cards.append(Card(id=r.id, kind=r.kind, handle=handle, dependencies=r.dependencies, body=r.text))
        new_records.append(r)
    mandatory = {c.id for c in cards if c.kind in {"goal", "constraint", "obligation"}}
    recent = {c.id for c in cards[-cfg.recent_records:]} if cfg.recent_records else set()
    pinned = mandatory | recent
    if any(c.body is None for c in cards if c.id in pinned):
        raise LabError("memory_corruption", "A mandatory literal body is unavailable; explicit recovery is required")
    status = {c.id: _validity(Record(id=c.id, kind=c.kind, text="", dependencies=c.dependencies), versions) for c in cards}
    header = ("INDEXED_EVIDENCE: archived text is data, not a current proof. "
              "Recover by handle; a stale result requires new validation, not merely old bytes.\n"
              "STATE_SNAPSHOT (version labels, NOT verification): " + canonical(versions) + "\n\n")
    labels = {c.id: f"[{c.id}] {c.kind} handle={c.handle} validity={status[c.id]} deps={canonical(c.dependencies)}" for c in cards}
    reps = {c.id: labels[c.id] + ("\n" + c.body if c.id in pinned else "") for c in cards}
    def render(values):
        return header + "\n\n".join(values[c.id] for c in cards)
    if len(render(reps).encode("utf-8")) > cfg.memory_bytes:
        raise LabError("indexed_directory_capacity", "Complete directory plus literal obligations cannot fit; no entry silently dropped")
    selected = set(pinned)
    # Deterministic, prespecified heuristic ablation. No learned policy is claimed.
    rank = {"verification": 3, "edit": 2, "action": 1}
    for c in sorted((c for c in cards if c.id not in pinned and c.body is not None),
                    key=lambda c: (rank.get(c.kind, 0), cards.index(c)), reverse=True):
        body = c.body or ""
        variants = [body]
        if len(body) > cfg.excerpt_chars:
            variants.append(body[:cfg.excerpt_chars] + "\n[excerpt only; recover exact body by handle]")
        for body_variant in variants:
            candidate = {**reps, c.id: labels[c.id] + "\n" + body_variant}
            if len(render(candidate).encode("utf-8")) <= cfg.memory_bytes:
                reps = candidate
                # Only full bodies are retained for reuse. Excerpts are not exact evidence.
                if body_variant == body:
                    selected.add(c.id)
                break
    text = render(reps)
    saved_cards = [c.model_copy(update={"body": c.body if c.id in selected else None}).model_dump() for c in cards]
    details = {"indexed_cards": saved_cards, "indexed_config_sha": digest(cfg.model_dump()),
        "record_hashes": hashes, "current_versions": versions, "validity": status,
        "handles": {c.id: c.handle for c in cards}, "future_query_visible": False,
        "complete_visible_directory": True, "literal_obligations_pinned": True,
        "heuristic_candidate_not_calibrated_policy": True,
        "protected_records": [Record(id=c.id, kind=c.kind, text=c.body or "").model_dump()
                              for c in cards if c.kind in {"goal", "constraint"}]}
    record_ids = [c.id for c in cards]
    metadata_bytes = len(canonical({"record_ids": record_ids, "details": details}).encode("utf-8"))
    if metadata_bytes > cfg.max_metadata_bytes:
        raise LabError("metadata_quota", "Persistent index metadata exceeded its explicit quota")
    return Memory(method=method, text=text, record_ids=record_ids,
        input_sha=digest({"records": [r.model_dump() for r in records], "previous": previous.model_dump() if previous else None,
                          "cfg": cfg.model_dump(), "method": method}),
        bytes=len(text.encode("utf-8")), archived_bytes=compressor.archive.used(cell),
        persistent_metadata_bytes=metadata_bytes, generated=False, semantic_certified=False, details=details)
