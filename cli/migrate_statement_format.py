"""
Migration: expand the accounts.statement_format CHECK constraint to include 'ofx'.

SQLite does not support ALTER COLUMN, so adding a new allowed value requires
recreating the accounts table with the updated constraint. This script is
idempotent: it detects whether the constraint already allows 'ofx' (by probing
with a dummy INSERT) and skips the migration if so.

Usage:
    python3 cli/migrate_statement_format.py
    python3 cli/migrate_statement_format.py --db-path /path/to/custom.db
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from persistence.database import get_db_path


def _needs_migration(conn: sqlite3.Connection) -> bool:
    """Return True if the accounts table CHECK still excludes 'ofx'."""
    try:
        conn.execute(
            "INSERT INTO accounts "
            "(account_code, display_name, account_type, currency, owner_id, statement_format) "
            "VALUES ('_ofx_probe_', '_probe_', 'checking', 'USD', "
            "(SELECT id FROM owners LIMIT 1), 'ofx')"
        )
        conn.execute("DELETE FROM accounts WHERE account_code='_ofx_probe_'")
        return False  # INSERT succeeded → constraint already allows 'ofx'
    except sqlite3.IntegrityError:
        return True   # CHECK rejected 'ofx' → migration needed


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_code TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            description TEXT,
            account_type TEXT NOT NULL CHECK(account_type IN ('checking','savings','credit_card','investment')),
            institution TEXT,
            account_number_last4 TEXT,
            currency TEXT NOT NULL,
            owner_id INTEGER NOT NULL REFERENCES owners(id),
            opening_balance_native REAL,
            opened_date TEXT,
            closed_date TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            statement_format TEXT CHECK(
                statement_format IN ('hsbc','chase_bank','sapphire','ofx')
                OR statement_format IS NULL
            ),
            created_at TEXT DEFAULT (datetime('now'))
        );

        INSERT INTO accounts_new SELECT * FROM accounts;
        DROP TABLE accounts;
        ALTER TABLE accounts_new RENAME TO accounts;
    """)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Expand statement_format CHECK to include 'ofx'")
    parser.add_argument("--db-path", default=None, help="Override DB path (default: PULSE_DB_PATH env or data/ledger.db)")
    args = parser.parse_args()

    db_path = args.db_path or get_db_path()
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path!r} — nothing to migrate.")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")

    if not _needs_migration(conn):
        print("Already up to date — statement_format constraint already includes 'ofx'.")
        conn.close()
        return

    print(f"Migrating {db_path!r} …")
    migrate(conn)
    print("Done. accounts.statement_format now accepts 'ofx'.")
    conn.close()


if __name__ == "__main__":
    main()
