"""
API/route smoke tests against a throwaway database (PULSE_DB_PATH points
at a temp file created at import time, before web.app is loaded).
"""
import io
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
_conn.execute("INSERT INTO categories (name, money_type) VALUES ('Income - Salary', 'income')")
_conn.execute("""
    INSERT INTO transactions (account_id, transaction_date, description,
        transaction_currency, transaction_amount, settlement_currency, settlement_amount,
        reporting_amount, category_id, money_type, confidence)
    VALUES (1, '2025-06-02', 'CORNER GROCER', 'GBP', -20.0, 'GBP', -20.0, -20.0, 1, 'expense', 'high'),
           (1, '2025-06-25', 'ACME PAYROLL', 'GBP', 500.0, 'GBP', 500.0, 500.0, 2, 'income', 'high')
""")
_conn.commit()
_conn.close()

from web.app import app  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.mark.parametrize("path", ["/", "/transactions", "/analysis", "/review", "/accounts", "/vendor-rules"])
def test_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_malformed_query_params_degrade_gracefully(client):
    assert client.get("/analysis?offset=abc").status_code == 200
    assert client.get("/?cov_offset=--").status_code == 200
    assert client.get("/transactions?account_id=zzz").status_code == 200


def test_api_404_returns_json(client):
    resp = client.get("/api/transactions/999999")
    assert resp.status_code == 404
    assert resp.get_json()["ok"] is False


def test_upload_rejects_unknown_file_type(client):
    resp = client.post(
        "/api/import/detect",
        data={"file": (io.BytesIO(b"not a pdf at all"), "fake.pdf")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "Unrecognised file type" in resp.get_json()["error"]


def test_import_queue_rejects_path_traversal_temp_id(client):
    resp = client.post("/api/import/queue", json={
        "temp_id": "../../etc/passwd", "account_type": "hsbc",
    })
    assert resp.status_code == 400
    assert "Invalid temp_id" in resp.get_json()["error"]


def test_filtered_totals_match_seeded_rows(client):
    resp = client.get("/transactions?year_month=2025-06")
    assert resp.status_code == 200
    from persistence.repositories import TransactionRepository
    conn = sqlite3.connect(os.environ["PULSE_DB_PATH"])
    conn.row_factory = sqlite3.Row
    totals = TransactionRepository(conn).totals_for_filtered(year_month="2025-06")
    conn.close()
    assert totals["income"] == 500.0
    assert totals["expense"] == 20.0
    assert totals["net"] == 480.0


def test_bulk_resolve_without_money_type_preserves_each_rows_value(client):
    conn = sqlite3.connect(os.environ["PULSE_DB_PATH"])
    conn.row_factory = sqlite3.Row
    before = conn.execute("SELECT id, money_type FROM transactions WHERE description='ACME PAYROLL'").fetchone()
    resp = client.post("/api/review/bulk-resolve", json={
        "txn_ids": [before["id"]], "category_id": 1, "money_type": None,
    })
    assert resp.status_code == 200
    after = conn.execute("SELECT money_type, category_id FROM transactions WHERE id=?", (before["id"],)).fetchone()
    conn.close()
    assert after["money_type"] == before["money_type"]  # untouched
    assert after["category_id"] == 1                     # updated
