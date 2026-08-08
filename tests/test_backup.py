"""
persistence/backup.py: VACUUM INTO produces an openable snapshot, pruning
keeps only the newest KEEP_BACKUPS files, and the reason string is
sanitized into a filesystem-safe slug.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Reuse whichever throwaway DB an earlier-imported test module already set
# PULSE_DB_PATH to — get_connection() reads this env var dynamically for
# every call, so standing up a second DB here would silently redirect every
# other test module's requests to it too. See tests/test_csrf.py for the
# same guard and full rationale.
if "PULSE_DB_PATH" not in os.environ:
    _db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    os.environ["PULSE_DB_PATH"] = _db_file.name

    from persistence.database import get_connection, init_schema  # noqa: E402

    _conn = get_connection()
    init_schema(_conn)
    _conn.execute("INSERT INTO owners (name) VALUES ('Demo User')")
    _conn.commit()
    _conn.close()

import sqlite3  # noqa: E402
import pytest  # noqa: E402

from persistence.backup import create_backup, backup_dir, KEEP_BACKUPS, _prune  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_backup_dir():
    yield
    d = backup_dir()
    if os.path.isdir(d):
        for f in os.listdir(d):
            os.remove(os.path.join(d, f))


def test_create_backup_produces_openable_db():
    dest = create_backup("test")
    assert os.path.exists(dest)
    conn = sqlite3.connect(dest)
    count = conn.execute("SELECT COUNT(*) FROM owners").fetchone()[0]
    conn.close()
    assert count == 1


def test_prune_keeps_four():
    for i in range(6):
        dest = create_backup("test")
        os.rename(dest, dest.replace(".db", f"-{i}.db"))
    _prune()
    remaining = [f for f in os.listdir(backup_dir()) if f.startswith("ledger-") and f.endswith(".db")]
    assert len(remaining) == KEEP_BACKUPS
    # the two oldest (suffix -0, -1) were pruned away
    assert not any(f.endswith("-0.db") or f.endswith("-1.db") for f in remaining)


def test_reason_slug_sanitized():
    dest = create_backup("Pre/Reset!!")
    assert "-prereset.db" in dest
