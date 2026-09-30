import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cli"))

from move_data import move_data, MoveError  # noqa: E402
from persistence.database import init_schema  # noqa: E402


def _make_ledger(folder):
    os.makedirs(folder)
    conn = sqlite3.connect(os.path.join(folder, "ledger.db"))
    conn.execute("PRAGMA journal_mode = WAL")
    init_schema(conn)
    conn.execute("INSERT INTO owners (name) VALUES ('Demo')")
    conn.execute("INSERT INTO categories (name, money_type) VALUES ('Groceries', 'expense')")
    conn.commit()
    conn.close()
    with open(os.path.join(folder, ".secret_key"), "w") as f:
        f.write("k" * 64)
    os.makedirs(os.path.join(folder, "backups"))
    with open(os.path.join(folder, "backups", "ledger-old.db"), "w") as f:
        f.write("x")


def test_copies_verifies_and_carries_key_and_backups(tmp_path):
    src, dest = str(tmp_path / "dev" / "data"), str(tmp_path / "server" / "data")
    _make_ledger(src)

    result = move_data(src, dest)

    assert result["counts"]["owners"] == 1
    assert result["counts"]["categories"] == 1
    assert open(os.path.join(dest, ".secret_key")).read() == "k" * 64
    assert os.path.isfile(os.path.join(dest, "backups", "ledger-old.db"))
    assert os.path.isfile(os.path.join(src, "ledger.db"))  # source untouched without the flag


def test_refuses_to_overwrite_existing_destination(tmp_path):
    src, dest = str(tmp_path / "a"), str(tmp_path / "b")
    _make_ledger(src)
    _make_ledger(dest)
    with pytest.raises(MoveError, match="refusing to overwrite"):
        move_data(src, dest)


def test_retire_source_renames_never_deletes(tmp_path):
    src, dest = str(tmp_path / "data"), str(tmp_path / "server" / "data")
    _make_ledger(src)

    result = move_data(src, dest, retire_source=True)

    assert not os.path.exists(src)
    assert os.path.isfile(os.path.join(result["retired_to"], "ledger.db"))


def test_missing_source_db_errors(tmp_path):
    with pytest.raises(MoveError, match="No ledger.db"):
        move_data(str(tmp_path / "nope"), str(tmp_path / "dest"))
