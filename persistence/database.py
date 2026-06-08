"""
Database connection management.

PREVIOUS DESIGN PROBLEM: every script (main.py, ingest_statement.py,
init_db.py, categorizer.py) called sqlite3.connect(DB_PATH) independently,
each hardcoding its own relative path to db/ledger.db. That meant the DB
path was duplicated in 4+ places, and there was no single point to add
connection-level behavior (foreign keys, row factory, WAL mode) without
editing every file.

FIX: one function, get_connection(), is the only place that opens a
connection. Everything else asks for one. Path is resolved once.
"""
import os
import sqlite3

_DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "ledger.db")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def get_db_path() -> str:
    """Allows override via PULSE_DB_PATH env var, e.g. for pointing tests
    at a throwaway database without touching the real one."""
    return os.environ.get("PULSE_DB_PATH", _DEFAULT_DB_PATH)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")   # prevents reader/writer lock contention
    conn.execute("PRAGMA synchronous = NORMAL")  # safe with WAL; faster than FULL
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    with open(_SCHEMA_PATH) as f:
        conn.executescript(f.read())
