"""
Consistent SQLite backups via VACUUM INTO (safe under WAL, unlike file copy).

Layout: <db_dir>/backups/ledger-YYYYMMDD-HHMMSS-<reason>.db
Retention: newest KEEP_BACKUPS files kept, older pruned.
Called manually (cli/backup.py) and automatically before destructive
operations (reset routes, statement imports).
"""
import logging
import os
import re
import sqlite3
from datetime import datetime

from persistence.database import get_db_path

KEEP_BACKUPS = 4
log = logging.getLogger("pulse.backup")


def backup_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(get_db_path())), "backups")


def create_backup(reason: str = "manual") -> str:
    """Snapshot the current DB; returns the backup file path. Prunes old backups.
    Raises sqlite3.Error on failure — callers decide whether that aborts them."""
    os.makedirs(backup_dir(), exist_ok=True)
    reason_slug = re.sub(r"[^a-z0-9_-]", "", reason.lower()) or "manual"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(backup_dir(), f"ledger-{stamp}-{reason_slug}.db")
    conn = sqlite3.connect(get_db_path())
    try:
        conn.execute("VACUUM INTO ?", (dest,))
    finally:
        conn.close()
    _prune()
    log.info("Backup written: %s", dest)
    return dest


def _prune() -> None:
    files = sorted(
        (f for f in os.listdir(backup_dir()) if f.startswith("ledger-") and f.endswith(".db")),
        reverse=True,  # names embed a sortable timestamp
    )
    for old in files[KEEP_BACKUPS:]:
        try:
            os.remove(os.path.join(backup_dir(), old))
        except OSError:
            log.warning("Could not prune old backup %s", old)
