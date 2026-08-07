"""
services/reconciliation.py: recording a snapshot from a ReconciliationResult
(including the computed-balance-None fallback and the both-None no-op), and
the human-readable report text.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from domain.models import ReconciliationResult  # noqa: E402
from persistence.database import init_schema  # noqa: E402
from persistence.repositories import SnapshotRepository  # noqa: E402
from services.reconciliation import record_snapshot, format_report  # noqa: E402


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_schema(c)
    c.execute("INSERT INTO owners (name) VALUES ('Demo User')")
    c.execute("""
        INSERT INTO accounts (account_code, display_name, account_type, currency, owner_id)
        VALUES ('UK_CURRENT', 'UK Current Account', 'checking', 'GBP', 1)
    """)
    c.commit()
    return c


@pytest.fixture
def repo(conn):
    return SnapshotRepository(conn)


def test_records_clean_snapshot(conn, repo):
    result = ReconciliationResult(computed_balance=100.0, stated_balance=100.0, diff=0.0)
    record_snapshot(repo, 1, "2025-06", result)
    row = conn.execute("SELECT * FROM account_snapshots WHERE account_id=1 AND year_month='2025-06'").fetchone()
    assert row["reconciled"] == 1
    assert row["closing_balance_native"] == 100.0


def test_records_gap(conn, repo):
    result = ReconciliationResult(computed_balance=105.0, stated_balance=100.0, diff=5.0)
    record_snapshot(repo, 1, "2025-06", result)
    row = conn.execute("SELECT * FROM account_snapshots WHERE account_id=1 AND year_month='2025-06'").fetchone()
    assert row["reconciled"] == 0
    assert row["reconciliation_diff"] == 5.0


def test_computed_none_falls_back_to_stated(conn, repo):
    result = ReconciliationResult(computed_balance=None, stated_balance=250.0, diff=None)
    record_snapshot(repo, 1, "2025-06", result)
    row = conn.execute("SELECT * FROM account_snapshots WHERE account_id=1 AND year_month='2025-06'").fetchone()
    assert row is not None
    assert row["closing_balance_native"] == 250.0


def test_both_none_records_nothing(conn, repo):
    result = ReconciliationResult(computed_balance=None, stated_balance=None, diff=None)
    record_snapshot(repo, 1, "2025-06", result)
    count = conn.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0]
    assert count == 0


def test_upsert_overwrites_same_month(conn, repo):
    record_snapshot(repo, 1, "2025-06", ReconciliationResult(computed_balance=100.0, stated_balance=100.0, diff=0.0))
    record_snapshot(repo, 1, "2025-06", ReconciliationResult(computed_balance=150.0, stated_balance=150.0, diff=0.0))
    rows = conn.execute("SELECT * FROM account_snapshots WHERE account_id=1 AND year_month='2025-06'").fetchall()
    assert len(rows) == 1
    assert rows[0]["closing_balance_native"] == 150.0


def test_format_report_mentions_mismatches():
    dirty = ReconciliationResult(computed_balance=100.0, stated_balance=100.0, diff=0.0,
                                  mismatches=[{"line": 3}])
    assert "DO NOT TRUST" in format_report("Test Account", dirty)

    clean = ReconciliationResult(computed_balance=100.0, stated_balance=100.0, diff=0.0)
    assert "EXACT TIE-OUT" in format_report("Test Account", clean)

    gap = ReconciliationResult(computed_balance=105.0, stated_balance=100.0, diff=5.0)
    assert "RECONCILIATION GAP" in format_report("Test Account", gap)
