"""
CSRF protection tests — mutating POST routes must reject requests without a
valid token and accept requests carrying one, either as a form field or as
the X-CSRF-Token header. GET requests are never affected.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Reuse whichever throwaway DB an earlier-imported test module (e.g.
# test_api.py) already set PULSE_DB_PATH to, since web.app's get_connection()
# reads this env var dynamically for every request — creating a second DB
# here would silently redirect *all* tests to it. Only stand up our own DB
# if nothing has claimed the env var yet.
if "PULSE_DB_PATH" not in os.environ:
    _db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    os.environ["PULSE_DB_PATH"] = _db_file.name

    from persistence.database import get_connection, init_schema  # noqa: E402

    _conn = get_connection()
    init_schema(_conn)
    _conn.execute("INSERT INTO owners (name) VALUES ('Demo User')")
    _conn.execute("""
        INSERT INTO accounts (account_code, display_name, account_type, currency, owner_id, statement_format)
        VALUES ('UK_CURRENT', 'UK Current Account', 'checking', 'GBP', 1, 'hsbc')
    """)
    _conn.execute("INSERT INTO categories (name, money_type) VALUES ('Groceries', 'expense')")
    _conn.execute("""
        INSERT INTO transactions (account_id, transaction_date, description,
            transaction_currency, transaction_amount, settlement_currency, settlement_amount,
            reporting_amount, category_id, money_type, confidence)
        VALUES (1, '2025-06-02', 'CORNER GROCER', 'GBP', -20.0, 'GBP', -20.0, -20.0, 1, 'expense', 'high')
    """)
    _conn.commit()
    _conn.close()

from web.app import app  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
def client():
    app.config["TESTING"] = False
    with app.test_client() as c:
        yield c


def _get_token(client):
    client.get("/")
    with client.session_transaction() as sess:
        return sess["_csrf_token"]


def test_post_without_token_rejected(client):
    resp = client.post("/categories/add", data={"name": "No Token Category", "money_type": "expense"})
    assert resp.status_code == 403


def test_post_with_form_token_accepted(client):
    token = _get_token(client)
    resp = client.post(
        "/categories/add",
        data={"name": "Form Token Category", "money_type": "expense", "csrf_token": token},
    )
    assert resp.status_code in (200, 302)


def test_post_with_header_token_accepted(client):
    token = _get_token(client)
    resp = client.post(
        "/api/review/bulk-resolve",
        json={"txn_ids": [1], "category_id": 1, "money_type": "expense"},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_get_requests_unaffected_by_csrf(client):
    assert client.get("/").status_code == 200
    assert client.get("/categories").status_code == 200
