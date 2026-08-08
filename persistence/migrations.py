"""
Versioned schema migrations.

Files: persistence/migrations/NNNN_description.sql, applied in filename order.
Tracking: schema_migrations table (filename UNIQUE).
Baseline rule: 0001_baseline.sql is the full schema as of the introduction of
this system. On a database that predates migrations (accounts table exists,
schema_migrations doesn't), 0001 is recorded as applied WITHOUT executing.
Rule for future changes: add a new NNNN_*.sql AND update schema.sql to match.
"""
import os
import sqlite3
from typing import List

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")


def _ensure_tracking_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL UNIQUE,
            applied_at TEXT DEFAULT (datetime('now'))
        )
    """)


def pending_migrations(conn: sqlite3.Connection) -> List[str]:
    _ensure_tracking_table(conn)
    applied = {r["filename"] if isinstance(r, sqlite3.Row) else r[0]
               for r in conn.execute("SELECT filename FROM schema_migrations")}
    files = sorted(f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql"))
    return [f for f in files if f not in applied]


def apply_migrations(conn: sqlite3.Connection) -> List[str]:
    """Apply all pending migrations in order; returns the list applied.
    Handles the pre-migrations baseline case."""
    _ensure_tracking_table(conn)
    pending = pending_migrations(conn)
    applied_now = []
    for fname in pending:
        if fname == "0001_baseline.sql" and _schema_already_exists(conn):
            conn.execute("INSERT INTO schema_migrations (filename) VALUES (?)", (fname,))
            applied_now.append(fname + " (baseline recorded, not executed)")
            continue
        with open(os.path.join(MIGRATIONS_DIR, fname)) as f:
            conn.executescript(f.read())
        conn.execute("INSERT INTO schema_migrations (filename) VALUES (?)", (fname,))
        applied_now.append(fname)
    conn.commit()
    return applied_now


def _schema_already_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
    ).fetchone() is not None
