"""
Copy a PULSE data folder (ledger.db + .secret_key + backups/) to a new
location, verifying the copy before anything else happens. Used by
scripts/windows/install-server.ps1 to move a real ledger out of a dev
checkout into the always-on server's own folder.

Safety rules:
  * Never overwrites: refuses if the destination already has a ledger.db.
  * Copies the database with SQLite's online backup API (a consistent
    snapshot, even under WAL), not a file copy.
  * Verifies PRAGMA integrity_check and an exact per-table row-count match
    before reporting success.
  * Never deletes the source. --retire-source only RENAMES it (to
    <folder>.moved-<timestamp>) so the dev checkout can't keep writing to a
    stale copy; delete it yourself once you're happy.

Run: python cli/move_data.py --src <dev>/data --dest <server>/data [--retire-source]
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

DB_NAME = "ledger.db"


class MoveError(Exception):
    pass


def _table_counts(conn: sqlite3.Connection) -> dict:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


def move_data(src: str, dest: str, retire_source: bool = False) -> dict:
    """Copy + verify. Returns {"counts": {...}, "retired_to": path-or-None}."""
    src_db = os.path.join(src, DB_NAME)
    dest_db = os.path.join(dest, DB_NAME)
    if not os.path.isfile(src_db):
        raise MoveError(f"No {DB_NAME} in {src}")
    if os.path.exists(dest_db):
        raise MoveError(f"{dest_db} already exists — refusing to overwrite it")
    if os.path.abspath(src) == os.path.abspath(dest):
        raise MoveError("Source and destination are the same folder")

    os.makedirs(dest, exist_ok=True)

    src_conn = sqlite3.connect(src_db)
    dest_conn = sqlite3.connect(dest_db)
    try:
        src_conn.backup(dest_conn)
        dest_conn.commit()
        integrity = dest_conn.execute("PRAGMA integrity_check").fetchone()[0]
        src_counts = _table_counts(src_conn)
        dest_counts = _table_counts(dest_conn)
    finally:
        src_conn.close()
        dest_conn.close()

    if integrity != "ok":
        os.remove(dest_db)
        raise MoveError(f"Integrity check failed on the copy: {integrity} (copy removed)")
    if src_counts != dest_counts:
        os.remove(dest_db)
        raise MoveError(f"Row counts differ after copy (copy removed): {src_counts} vs {dest_counts}")

    # Session secret (keeps existing browser sessions valid) and old backups.
    key = os.path.join(src, ".secret_key")
    if os.path.isfile(key) and not os.path.exists(os.path.join(dest, ".secret_key")):
        shutil.copy2(key, os.path.join(dest, ".secret_key"))
    backups = os.path.join(src, "backups")
    if os.path.isdir(backups):
        shutil.copytree(backups, os.path.join(dest, "backups"), dirs_exist_ok=True)

    retired_to = None
    if retire_source:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        retired_to = f"{os.path.abspath(src).rstrip(os.sep)}.moved-{stamp}"
        try:
            os.rename(src, retired_to)
        except OSError as e:
            raise MoveError(
                f"Copy verified at {dest_db}, but could not rename {src} ({e}). "
                "Is the dev server still running? Stop it and rename the folder yourself."
            )
    return {"counts": dest_counts, "retired_to": retired_to}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True, help="existing data folder (contains ledger.db)")
    parser.add_argument("--dest", required=True, help="new data folder")
    parser.add_argument("--retire-source", action="store_true",
                        help="rename the source folder after a verified copy (never deletes)")
    args = parser.parse_args()
    try:
        result = move_data(args.src, args.dest, args.retire_source)
    except MoveError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Copied and verified -> {os.path.join(args.dest, DB_NAME)}")
    print("  integrity_check: ok")
    for table, n in result["counts"].items():
        print(f"  {table}: {n} rows (matches source)")
    if result["retired_to"]:
        print(f"Source renamed to {result['retired_to']} (not deleted).")


if __name__ == "__main__":
    main()
