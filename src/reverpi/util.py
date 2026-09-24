"""Small, deliberately boring persistence and identity primitives."""
from __future__ import annotations
import contextlib
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterator


def canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def strict_json_loads(value: str | bytes | bytearray) -> Any:
    """Reject duplicate keys and non-finite constants instead of changing meaning."""
    def pairs(items):
        result = {}
        for key, val in items:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = val
        return result
    def constant(_value):
        raise ValueError("Non-finite JSON constant")
    def finite_float(text):
        number = float(text)
        if not math.isfinite(number):
            raise ValueError("JSON number exceeds finite floating-point range")
        return number
    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float)


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def bytes_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def seal_cache(payload: Any) -> str:
    """Same-row checksum for accidental cache corruption, not an authenticity proof."""
    return canonical({"cache_schema": 1, "payload": payload, "sha256": digest(payload)})


def unseal_cache(value: str) -> Any:
    from .errors import LabError
    try:
        obj = strict_json_loads(value)
        if (not isinstance(obj, dict) or set(obj) != {"cache_schema", "payload", "sha256"}
                or type(obj["cache_schema"]) is not int or obj["cache_schema"] != 1
                or obj["sha256"] != digest(obj["payload"])):
            raise ValueError("Cache checksum or schema differs")
        return obj["payload"]
    except (ValueError, TypeError, RecursionError) as exc:
        raise LabError("cache_integrity", "Cached state is changed, corrupt or legacy-unsealed; do not silently replay it") from exc


def safe_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) or value in {".", ".."}:
        raise ValueError("Unsafe identifier")
    return value


def atomic_write(path: Path, data: str | bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    b = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(b)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if os.name == "posix":
            d = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(d)
            finally:
                os.close(d)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_create(path: Path, data: str | bytes, *, mode: int = 0o600) -> None:
    """Publish a complete NEW file without ever replacing an existing destination.

    POSIX hard-link publication is atomic and preserves O_EXCL semantics. Temporary
    bytes have private permissions before writing. This is not a multi-file commit.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    value = data.encode("utf-8") if isinstance(data, str) else data
    fd, name = tempfile.mkstemp(prefix=".pending-create-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(name, path, follow_symlinks=False)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def contained_regular_file(root: Path, relative: str) -> Path:
    """Validate a STATIC evidence-tree path before reading. No hostile-writer guarantee."""
    from pathlib import PurePosixPath
    if not isinstance(relative, str):
        raise ValueError("Evidence path must be a string")
    part = PurePosixPath(relative)
    if (not part.parts or part.is_absolute() or any(x in {".", ".."} for x in part.parts)
            or str(part) != relative or "\\" in relative or ":" in relative):
        raise ValueError("Unsafe evidence path")
    base = root.resolve(strict=True)
    path = base
    for component in part.parts:
        path = path / component
        if path.is_symlink():
            raise ValueError("Symlinked evidence path")
    if not path.is_file() or not path.resolve(strict=True).is_relative_to(base):
        raise ValueError("Evidence is missing or not a regular in-root file")
    return path


@contextlib.contextmanager
def process_lock(path: Path) -> Iterator[None]:
    """One writer per study; SQLite separately protects monetary reservations."""
    if os.name != "posix":
        raise RuntimeError("Study/gateway execution requires Linux/macOS or WSL2")
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("A writer already owns this study directory") from exc
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def source_manifest(root: Path) -> dict[str, str]:
    """Only reviewable implementation inputs, never credentials/run traces/bench gold."""
    paths: list[Path] = []
    for folder in ("src", "tests", "pi/src", "pi/tests", "scripts"):
        base = root / folder
        if base.is_symlink():
            raise ValueError("Implementation directories cannot be mutable symlinks")
        if base.exists():
            paths.extend(base.rglob("*"))
    paths.extend(root / name for name in ("pyproject.toml", "requirements.lock", "requirements-harbor.lock", "pi/package.json", "pi/package-lock.json", "pi/tsconfig.json", "environment.lock.json")
                 if (root / name).exists())
    if any(p.is_symlink() for p in paths):
        raise ValueError("Implementation inputs cannot be mutable symlinks")
    return {str(p.relative_to(root)): bytes_digest(p.read_bytes()) for p in sorted(set(paths))
            if p.is_file() and not p.is_symlink()
            and not any(x in {"__pycache__", "node_modules", "dist"}
                        or x.endswith((".egg-info", ".dist-info")) for x in p.relative_to(root).parts)}


def json_object(text: str) -> dict[str, Any]:
    """Strict output parser. No regex salvage of a partial model completion."""
    value = strict_json_loads(text)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value
