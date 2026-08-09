"""
ImportJobRepository: durable job records that survive a server restart.
Uses a temp-file DB (not in-memory) matching tests/test_api.py's convention,
since a real deployment's job status must be readable from a fresh
connection, not just the one that wrote it.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if "PULSE_DB_PATH" not in os.environ:
    _db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    os.environ["PULSE_DB_PATH"] = _db_file.name

    from persistence.database import get_connection, init_schema  # noqa: E402

    _conn = get_connection()
    init_schema(_conn)
    _conn.execute("INSERT INTO owners (name) VALUES ('Demo User')")
    _conn.commit()
    _conn.close()

import pytest  # noqa: E402

from persistence.database import get_connection  # noqa: E402
from persistence.repositories import ImportJobRepository  # noqa: E402


@pytest.fixture
def conn():
    c = get_connection()
    yield c
    c.execute("DELETE FROM import_jobs")
    c.commit()
    c.close()


@pytest.fixture
def repo(conn):
    return ImportJobRepository(conn)


def test_create_and_list(conn, repo):
    repo.create("job1", "temp1", "statement.pdf", "hsbc", 2025, 1, None)
    conn.commit()
    rows = repo.list_recent()
    assert len(rows) == 1
    assert rows[0]["status"] == "queued"
    assert rows[0]["filename"] == "statement.pdf"


def test_lifecycle(conn, repo):
    repo.create("job2", "temp2", "statement2.pdf", "chase_bank", 2025, 4, None)
    repo.mark_running("job2")
    repo.mark_done("job2", 5, 2, True, 0.0)
    conn.commit()
    row = repo.list_recent()[0]
    assert row["status"] == "done"
    assert row["inserted"] == 5
    assert row["skipped"] == 2
    assert row["reconciled"] == 1
    assert row["finished_at"] is not None


def test_mark_error(conn, repo):
    repo.create("job3", "temp3", "bad.pdf", "hsbc", None, None, None)
    repo.mark_error("job3", "boom")
    conn.commit()
    row = repo.list_recent()[0]
    assert row["status"] == "error"
    assert row["error"] == "boom"


def test_fail_stale(conn, repo):
    repo.create("job4", "temp4", "a.pdf", "hsbc", None, None, None)
    repo.create("job5", "temp5", "b.pdf", "hsbc", None, None, None)
    repo.mark_running("job5")
    repo.create("job6", "temp6", "c.pdf", "hsbc", None, None, None)
    repo.mark_done("job6", 1, 0, True, 0.0)
    conn.commit()

    count = repo.fail_stale()
    conn.commit()
    assert count == 2

    rows = {r["job_id"]: r for r in repo.list_recent()}
    assert rows["job4"]["status"] == "error"
    assert "restart" in rows["job4"]["error"].lower()
    assert rows["job5"]["status"] == "error"
    assert rows["job6"]["status"] == "done"
