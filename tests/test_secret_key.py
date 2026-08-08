"""
web.app._load_or_create_secret_key: persisted-file fallback when
PULSE_SECRET_KEY isn't set, so sessions/CSRF survive restarts and are
shared across multiple worker processes (see PR #13 for the bug this
fixes — each gunicorn worker previously generated its own random key).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from web.app import _load_or_create_secret_key  # noqa: E402


@pytest.fixture
def db_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("PULSE_DB_PATH", os.path.join(d, "ledger.db"))
        yield d


def test_explicit_env_var_wins(monkeypatch, db_dir):
    monkeypatch.setenv("PULSE_SECRET_KEY", "my-explicit-key")
    assert _load_or_create_secret_key() == "my-explicit-key"
    assert not os.path.exists(os.path.join(db_dir, ".secret_key"))


def test_generates_and_persists_key(monkeypatch, db_dir):
    monkeypatch.delenv("PULSE_SECRET_KEY", raising=False)
    key = _load_or_create_secret_key()
    key_path = os.path.join(db_dir, ".secret_key")
    assert os.path.exists(key_path)
    with open(key_path) as f:
        assert f.read().strip() == key


def test_reuses_persisted_key_across_calls(monkeypatch, db_dir):
    monkeypatch.delenv("PULSE_SECRET_KEY", raising=False)
    first = _load_or_create_secret_key()
    second = _load_or_create_secret_key()
    assert first == second
