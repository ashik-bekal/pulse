"""
persistence/migrations.py: applying the baseline to a fresh DB, idempotency,
and the pre-migrations upgrade path (an existing DB that predates this
system gets the baseline recorded without being re-executed).
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from persistence.migrations import apply_migrations, pending_migrations  # noqa: E402

SCHEMA_SQL_PATH = os.path.join(os.path.dirname(__file__), "..", "persistence", "schema.sql")


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def test_fresh_db_applies_baseline():
    conn = _conn()
    applied = apply_migrations(conn)
    assert applied[0].startswith("0001_baseline.sql")
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
    ).fetchone() is not None
    count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert count >= 1


def test_idempotent():
    conn = _conn()
    apply_migrations(conn)
    assert apply_migrations(conn) == []


def test_pre_migrations_db_records_baseline_without_executing():
    conn = _conn()
    with open(SCHEMA_SQL_PATH) as f:
        conn.executescript(f.read())
    applied = apply_migrations(conn)
    assert conn.execute(
        "SELECT 1 FROM schema_migrations WHERE filename='0001_baseline.sql'"
    ).fetchone() is not None
    assert any("baseline recorded" in entry for entry in applied)


def test_pending_migrations_lists_unapplied():
    conn = _conn()
    assert "0001_baseline.sql" in pending_migrations(conn)
    apply_migrations(conn)
    assert pending_migrations(conn) == []
