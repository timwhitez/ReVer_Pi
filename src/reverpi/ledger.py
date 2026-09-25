"""SQLite reservations: unknown attempts remain charged at the planning upper bound.

A process crash does not create permission to repeat a paid operation. Operators
must explicitly reconcile the ambiguous attempt or create a NEW study generation.
"""
from __future__ import annotations
import contextlib
import json
import math
import os
import sqlite3
import shutil
import time
from pathlib import Path
from typing import Any, Iterator
from .config import Budget
from .errors import LabError
from .util import canonical, seal_cache, unseal_cache

NANO = 1_000_000_000


def nanos(usd: float) -> int:
    if not math.isfinite(usd) or usd < 0:
        raise ValueError("Invalid cost")
    return math.ceil(usd * NANO)


# Columns the accounting queries depend on. A file lacking any of them is not a
# ReVer-Pi ledger and is rejected instead of being "repaired" into an empty one.
REQUIRED_SCHEMA = {
    "meta": {"key", "value"},
    "operations": {"op", "payload_sha", "cell", "state", "result", "error", "created"},
    "attempts": {"id", "op", "cell", "provider", "state", "started", "reserve_tokens", "reserve_nano",
                 "actual_tokens", "actual_nano", "raw_usage", "request_id", "status", "error_kind"},
}


def _totals(db: sqlite3.Connection, cell: str | None = None) -> dict:
    t, n, count = Ledger._spent(db, cell)
    clause, args = (" WHERE cell=?", (cell,)) if cell is not None else ("", ())
    known = db.execute("SELECT COALESCE(SUM(actual_tokens),0),COALESCE(SUM(actual_nano),0),"
                       "COALESCE(SUM(actual_tokens IS NULL),0),COALESCE(SUM(actual_nano IS NULL),0) FROM attempts" + clause, args).fetchone()
    return {"attempts": count, "accounted_tokens": t, "accounted_usd": n / NANO,
            "known_tokens": known[0], "known_usd": known[1] / NANO, "unknown_attempts": known[2], "unknown_currency_attempts": known[3], "currency_is_fully_known": known[3] == 0}


@contextlib.contextmanager
def _read_only(path: Path) -> Iterator[sqlite3.Connection]:
    """Open an EXISTING ledger without creating, migrating or re-binding anything.

    mode=ro never writes ledger content; SQLite may still maintain its own -wal/-shm
    sidecars so that a consistent snapshot of an active WAL ledger can be read.
    """
    path = Path(path)
    if not path.is_file():
        raise LabError("ledger_missing", "No ledger exists at this path; nothing was created")
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30, isolation_level=None)
    except sqlite3.Error as exc:
        raise LabError("ledger_unreadable", "Ledger could not be opened read-only") from exc
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=30000")
        for table, columns in REQUIRED_SCHEMA.items():
            present = {r["name"] for r in db.execute("SELECT name FROM pragma_table_info(?)", (table,))}
            if not columns <= present:
                raise LabError("ledger_schema", "Not a ReVer-Pi ledger (missing or incompatible schema)")
        if db.execute("SELECT 1 FROM meta WHERE key='budget'").fetchone() is None:
            raise LabError("ledger_schema", "Ledger has no bound budget identity")
        db.execute("BEGIN")  # One snapshot for every query below.
        yield db
        db.execute("COMMIT")
    except sqlite3.DatabaseError as exc:
        raise LabError("ledger_schema", "Not a readable ReVer-Pi ledger") from exc
    finally:
        db.close()


def read_totals(path: Path, cell: str | None = None) -> dict:
    """Read-only accounting summary of an existing ledger; needs no runtime config."""
    with _read_only(path) as db:
        return _totals(db, cell)


def bound_budget(path: Path) -> Budget:
    """The budget an existing ledger was created with, read without writing."""
    with _read_only(path) as db:
        row = db.execute("SELECT value FROM meta WHERE key='budget'").fetchone()
    try:
        return Budget.model_validate(json.loads(row["value"]))
    except ValueError as exc:
        raise LabError("ledger_schema", "Ledger budget identity is not valid") from exc


class Ledger:
    @classmethod
    def open_existing(cls, path: Path) -> "Ledger":
        """Writable handle on an existing ledger using its own bound budget (e.g. reconcile)."""
        return cls(path, bound_budget(path))

    def __init__(self, path: Path, budget: Budget):
        self.path, self.budget = Path(path), budget
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(
              op TEXT PRIMARY KEY, payload_sha TEXT NOT NULL, cell TEXT NOT NULL,
              state TEXT NOT NULL, result TEXT, error TEXT, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(
              id INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT NOT NULL, cell TEXT NOT NULL,
              provider TEXT NOT NULL, state TEXT NOT NULL, started REAL NOT NULL,
              reserve_tokens INTEGER NOT NULL, reserve_nano INTEGER NOT NULL,
              actual_tokens INTEGER, actual_nano INTEGER, raw_usage TEXT, request_id TEXT,
              status INTEGER, error_kind TEXT);
            CREATE INDEX IF NOT EXISTS attempts_cell ON attempts(cell);
            CREATE INDEX IF NOT EXISTS attempts_started ON attempts(provider,started);
            CREATE TABLE IF NOT EXISTS providers(
              name TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
              blocked_until REAL NOT NULL DEFAULT 0, model TEXT);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,event TEXT NOT NULL,created REAL NOT NULL);
            ''')
        os.chmod(self.path, 0o600)
        self.bind("budget", budget.model_dump())

    @contextlib.contextmanager
    def db(self, write=False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.execute("COMMIT")
        except BaseException:
            if write and db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def bind(self, key: str, value: Any):
        val = canonical(value)
        with self.db(True) as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if row and row[0] != val:
                raise LabError("identity_changed", f"Frozen {key} changed; use a new run directory")
            db.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", (key, val))

    def claim(self, op: str, payload_sha: str, cell: str) -> dict | None:
        with self.db(True) as db:
            row = db.execute("SELECT * FROM operations WHERE op=?", (op,)).fetchone()
            if row:
                if row["payload_sha"] != payload_sha or row["cell"] != cell:
                    raise LabError("idempotency_conflict", "Operation ID was reused with a different payload or cell")
                if row["state"] == "complete":
                    return unseal_cache(row["result"])
                if row["state"] == "failed":
                    e = json.loads(row["error"])
                    raise LabError(**e)
                raise LabError("operation_in_doubt", "Operation is running or was interrupted; not resent automatically", ambiguous=True)
            db.execute("INSERT INTO operations VALUES (?,?,?,?,NULL,NULL,?)",
                       (op, payload_sha, cell, "running", time.time()))
        return None

    def finish(self, op: str, result: dict | None = None, error: LabError | None = None):
        if (result is None) == (error is None):
            raise ValueError("Exactly one result or error is required")
        with self.db(True) as db:
            row = db.execute("SELECT state FROM operations WHERE op=?", (op,)).fetchone()
            if not row or row[0] != "running":
                raise LabError("ledger_state", "Can only finalize a running operation")
            db.execute("UPDATE operations SET state=?,result=?,error=? WHERE op=?",
                       ("failed" if error else "complete", seal_cache(result) if result is not None else None,
                        canonical(error.record()) if error else None, op))

    @staticmethod
    def _spent(db, cell=None):
        clause, args = (" WHERE cell=?", (cell,)) if cell is not None else ("", ())
        return db.execute("SELECT COALESCE(SUM(COALESCE(actual_tokens,reserve_tokens)),0),"
                          "COALESCE(SUM(COALESCE(actual_nano,reserve_nano)),0), COUNT(*) FROM attempts" + clause,
                          args).fetchone()

    def stop_reason(self) -> str | None:
        with self.db() as db:
            if db.execute("SELECT 1 FROM meta WHERE key='reservation_breach'").fetchone():
                return "reservation_breach"
            row = db.execute("SELECT value FROM meta WHERE key='operator_stop'").fetchone()
            return json.loads(row[0]) if row else None

    def stop(self, reason: str):
        if not reason or len(reason) > 120:
            raise ValueError("A bounded stop reason is required")
        with self.db(True) as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('operator_stop',?)", (canonical(reason),))
        # A stopped generation is deliberately immutable. Resume only by fixing
        # configuration/evidence and creating an explicitly new generation.

    def _reservation_cost(self, tokens: int, usd: float) -> int:
        if type(tokens) is not int or tokens <= 0:
            raise ValueError("Reservation must be positive")
        if shutil.disk_usage(self.path.parent).free < self.budget.min_free_disk_bytes:
            raise LabError("disk_guard", "Insufficient free disk for durable request/usage records; no request dispatched")
        return nanos(usd)

    def reserve(self, op: str, cell: str, provider: str, tokens: int, usd: float) -> int:
        """Low-level accounting reservation. Provider dispatch MUST use reserve_gated."""
        cost = self._reservation_cost(tokens, usd)
        with self.db(True) as db:
            return self._reserve(db, op, cell, provider, tokens, cost)

    def reserve_gated(self, op: str, cell: str, provider: str, tokens: int, usd: float,
                      rpm: int, tpm: int) -> tuple[int | None, float]:
        """Atomic shared-ledger rate admission AND spend reservation, across processes.

        A denied rate slot does not create a paid attempt. The caller sleeps outside
        the transaction and rechecks all gates before dispatching.
        """
        cost = self._reservation_cost(tokens, usd)
        with self.db(True) as db:
            if db.execute("SELECT 1 FROM meta WHERE key IN ('reservation_breach','operator_stop')").fetchone():
                raise LabError("run_stopped", "This generation is stopped; do not dispatch another request")
            delay = self._throttle_delay(db, provider, tokens, rpm, tpm)
            if delay > 0:
                return None, delay
            return self._reserve(db, op, cell, provider, tokens, cost), 0.0

    def _reserve(self, db, op, cell, provider, tokens, cost) -> int:
        if db.execute("SELECT 1 FROM meta WHERE key IN ('reservation_breach','operator_stop')").fetchone():
            raise LabError("run_stopped", "This generation is stopped; do not dispatch another request")
        total_t, total_c, total_n = self._spent(db)
        cell_t, cell_c, cell_n = self._spent(db, cell)
        b = self.budget
        if total_n >= b.max_attempts or cell_n >= b.per_cell_attempts or total_t + tokens > b.max_total_tokens or cell_t + tokens > b.per_cell_tokens:
            raise LabError("budget_exhausted", "Token/attempt reservation would exceed the frozen budget")
        if (b.max_total_usd is not None and total_c + cost > nanos(b.max_total_usd)) or (
                b.per_cell_usd is not None and cell_c + cost > nanos(b.per_cell_usd)):
            raise LabError("budget_exhausted", "Currency reservation would exceed the frozen budget")
        row = db.execute("SELECT state,cell FROM operations WHERE op=?", (op,)).fetchone()
        if not row or row[0] != "running" or row[1] != cell:
            raise LabError("ledger_state", "Reserve requires the claimed operation and its original cell")
        cursor = db.execute("INSERT INTO attempts(op,cell,provider,state,started,reserve_tokens,reserve_nano) VALUES (?,?,?,?,?,?,?)",
                            (op, cell, provider, "reserved", time.time(), tokens, cost))
        return int(cursor.lastrowid)


    def settle(self, attempt: int, *, tokens: int | None, usd: float | None, raw_usage=None,
               request_id=None, status=None, error_kind=None):
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise ValueError("Negative token usage")
        with self.db(True) as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
            if not row or row["state"] != "reserved":
                raise LabError("ledger_state", "Attempt already settled or absent")
            db.execute("UPDATE attempts SET state=?,actual_tokens=?,actual_nano=?,raw_usage=?,request_id=?,status=?,error_kind=? WHERE id=?",
                       ("known" if tokens is not None else "unknown", tokens, nanos(usd) if usd is not None else None,
                        canonical(raw_usage) if raw_usage is not None else None,
                        str(request_id)[:300] if request_id else None, status, error_kind, attempt))
            # If a provider exceeds the estimate, record reality and STOP future spending.
            if (tokens is not None and tokens > row["reserve_tokens"]) or (usd is not None and nanos(usd) > row["reserve_nano"]):
                db.execute("INSERT INTO audit(event,created) VALUES (?,?)",
                           (canonical({"kind": "reservation_underestimate", "attempt": attempt,
                                       "reserved": row["reserve_tokens"], "actual": tokens}), time.time()))
                db.execute("INSERT OR REPLACE INTO meta VALUES ('reservation_breach','true')")

    def throttle_delay(self, provider: str, tokens: int, rpm: int, tpm: int) -> float:
        """Read-only estimate; never use this alone as dispatch authorization."""
        with self.db() as db:
            return self._throttle_delay(db, provider, tokens, rpm, tpm)

    @staticmethod
    def _throttle_delay(db, provider: str, tokens: int, rpm: int, tpm: int) -> float:
        if any(type(x) is not int or x <= 0 for x in (tokens, rpm, tpm)):
            raise ValueError("Rate limits and token reservation must be positive integers")
        if tokens > tpm:
            raise LabError("rate_configuration", "One conservative request exceeds configured TPM; increase TPM or reduce context")
        if db.execute("SELECT 1 FROM meta WHERE key='reservation_breach'").fetchone():
            raise LabError("reservation_breach", "Observed usage exceeded a bound; recalibration in a new run is required")
        now = time.time()
        p = db.execute("SELECT blocked_until FROM providers WHERE name=?", (provider,)).fetchone()
        delay = max(0, p[0] - now) if p else 0
        rows = db.execute("SELECT started,reserve_tokens FROM attempts WHERE provider=? AND started>? ORDER BY started",
                          (provider, now - 60)).fetchall()
        remaining = sum(r[1] for r in rows)
        n = len(rows)
        for r in rows:
            if n < rpm and remaining + tokens <= tpm:
                break
            delay = max(delay, r[0] + 60 - now + .01)
            remaining -= r[1]
            n -= 1
        return delay


    def cooldown(self, provider: str, seconds: float, *, failure=False, threshold=5, circuit_seconds=60):
        with self.db(True) as db:
            db.execute("INSERT OR IGNORE INTO providers(name) VALUES (?)", (provider,))
            db.execute("UPDATE providers SET failures=failures+?,blocked_until=MAX(blocked_until,?) WHERE name=?",
                       (int(failure), time.time() + seconds, provider))
            count = db.execute("SELECT failures FROM providers WHERE name=?", (provider,)).fetchone()[0]
            if count >= threshold:
                db.execute("UPDATE providers SET blocked_until=MAX(blocked_until,?) WHERE name=?",
                           (time.time() + circuit_seconds, provider))

    def observed_model(self, provider: str, model: str, expected: str | None):
        if expected and model != expected:
            raise LabError("model_drift", "Response model differs from configured expected identity")
        with self.db(True) as db:
            db.execute("INSERT OR IGNORE INTO providers(name) VALUES (?)", (provider,))
            prev = db.execute("SELECT model FROM providers WHERE name=?", (provider,)).fetchone()[0]
            if prev and prev != model:
                raise LabError("model_drift", "Response model changed within this run")
            db.execute("UPDATE providers SET model=?,failures=0 WHERE name=?", (model, provider))

    def reconcile(self, attempt: int, tokens: int, usd: float, evidence: str):
        if type(tokens) is not int or tokens < 0 or not evidence.strip():
            raise ValueError("Reconciliation needs nonnegative usage and an external evidence reference")
        with self.db(True) as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
            currency_only = bool(row and row["state"] == "known" and row["actual_nano"] is None)
            if not row or (row["state"] not in {"unknown", "reserved"} and not currency_only):
                raise ValueError("Only unknown/interrupted or currency-incomplete attempts can be reconciled")
            if currency_only and tokens != row["actual_tokens"]:
                raise ValueError("Currency-only reconciliation cannot rewrite known token usage")
            db.execute("UPDATE attempts SET state='reconciled',actual_tokens=?,actual_nano=? WHERE id=?",
                       (tokens, nanos(usd), attempt))
            if tokens > row["reserve_tokens"] or nanos(usd) > row["reserve_nano"]:
                db.execute("INSERT OR REPLACE INTO meta VALUES ('reservation_breach','true')")
            db.execute("INSERT INTO audit(event,created) VALUES (?,?)",
                       (canonical({"kind": "manual_reconcile", "attempt": attempt, "evidence": evidence}), time.time()))
        # Deliberately does NOT make the operation re-executable.

    def totals(self, cell: str | None = None) -> dict:
        with self.db() as db:
            return _totals(db, cell)

    def attempts(self) -> list[dict]:
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM attempts ORDER BY id")]
