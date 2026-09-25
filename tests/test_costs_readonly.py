"""`reverpi costs` is a read-only ledger query (issue #11). Temporary SQLite only."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
import pytest
from reverpi.config import Budget
from reverpi.errors import LabError
from reverpi.ledger import Ledger, read_totals, bound_budget

ROOT = Path(__file__).resolve().parents[1]


def cli(*args, cwd):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run([sys.executable, "-B", "-m", "reverpi", *args], cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=60)
    return r.returncode, (json.loads(r.stdout) if r.stdout.strip() else None), r.stderr


def snapshot(path: Path):
    """Business content, bindings and mode of the ledger file."""
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        dump = list(db.iterdump())
    return dump, oct(path.stat().st_mode & 0o777)


CUSTOM = Budget(max_total_tokens=12345, per_cell_tokens=999, max_attempts=7, per_cell_attempts=3)


def populated(tmp_path) -> Path:
    path = tmp_path / "run" / "ledger.sqlite"
    ledger = Ledger(path, CUSTOM)
    ledger.claim("op1", "sha1", "cell-a")
    ledger.claim("op2", "sha2", "cell-b")
    known = ledger.reserve("op1", "cell-a", "p", 100, 0.0)
    ledger.settle(known, tokens=40, usd=None)
    ledger.reserve("op2", "cell-b", "p", 200, 0.0)  # Dispatched, never settled: unknown.
    return path


def test_missing_run_is_an_error_and_creates_nothing(tmp_path):
    run = tmp_path / "typo"
    code, out, err = cli("costs", "--run", str(run), cwd=tmp_path)
    assert code == 2 and out is None
    assert json.loads(err)["error"]["kind"] == "ledger_missing"
    assert not run.exists()


def test_missing_db_in_existing_dir_is_not_created(tmp_path):
    (tmp_path / "run").mkdir()
    with pytest.raises(LabError) as err:
        read_totals(tmp_path / "run" / "ledger.sqlite")
    assert err.value.kind == "ledger_missing"
    assert list((tmp_path / "run").iterdir()) == []


@pytest.mark.parametrize("content", [b"", b"not a database" * 64])
def test_non_sqlite_file_is_rejected_not_repaired(tmp_path, content):
    path = tmp_path / "ledger.sqlite"
    path.write_bytes(content)
    with pytest.raises(LabError) as err:
        read_totals(path)
    assert err.value.kind == "ledger_schema"
    assert path.read_bytes() == content


def test_foreign_sqlite_schema_is_rejected_without_adding_tables(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE attempts(id INTEGER)")
    with pytest.raises(LabError) as err:
        read_totals(path)
    assert err.value.kind == "ledger_schema"
    with sqlite3.connect(path) as db:
        assert [r[0] for r in db.execute("SELECT name FROM sqlite_master")] == ["attempts"]


def test_custom_budget_ledger_reads_without_config_and_is_unchanged(tmp_path):
    path = populated(tmp_path)
    os.chmod(path, 0o600)
    before = snapshot(path)
    # Run from a directory without configs/pilot.yaml: no runtime config is loaded.
    code, out, err = cli("costs", "--run", str(path.parent), cwd=tmp_path)
    assert code == 0, err
    assert out["attempts"] == 2 and out["known_tokens"] == 40 and out["unknown_attempts"] == 1
    assert out["accounted_tokens"] == 240 and out["currency_is_fully_known"] is False
    assert snapshot(path) == before
    assert bound_budget(path) == CUSTOM


def test_cell_filter(tmp_path):
    path = populated(tmp_path)
    assert read_totals(path, "cell-a")["attempts"] == 1
    assert read_totals(path, "cell-b")["unknown_attempts"] == 1


def test_consistent_read_of_an_active_wal_ledger(tmp_path):
    path = populated(tmp_path)
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE attempts SET actual_tokens=1 WHERE op='op2'")
        # Uncommitted writer state is not visible; the last committed snapshot is.
        assert read_totals(path)["unknown_attempts"] == 1
        writer.execute("COMMIT")
        assert read_totals(path)["unknown_attempts"] == 0
    finally:
        writer.close()


def test_reconcile_is_explicit_write_using_the_bound_budget(tmp_path):
    path = populated(tmp_path)
    unknown = next(a["id"] for a in Ledger(path, CUSTOM).attempts() if a["op"] == "op2")
    code, out, err = cli("reconcile", "--run", str(path.parent), "--attempt", str(unknown), "--tokens", "150",
                         "--usd", "0", "--evidence", "invoice-123", cwd=tmp_path)
    assert code == 0, err
    assert out["unknown_attempts"] == 0
    with sqlite3.connect(path) as db:
        events = [json.loads(r[0]) for r in db.execute("SELECT event FROM audit")]
    assert events[-1]["kind"] == "manual_reconcile" and events[-1]["evidence"] == "invoice-123"


def test_reconcile_never_creates_a_ledger(tmp_path):
    run = tmp_path / "typo"
    code, _, err = cli("reconcile", "--run", str(run), "--attempt", "1", "--tokens", "1", "--usd", "0",
                       "--evidence", "x", cwd=ROOT)
    assert code == 2 and json.loads(err)["error"]["kind"] == "ledger_missing"
    assert not run.exists()
