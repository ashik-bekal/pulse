"""
End-to-end pipeline test on synthetic statement text: parser -> parser-level
reconciliation -> ingestion (categorisation, review queue, persistence) ->
reconciliation snapshot, all against an in-memory database.

This is the committed replacement for the local-only
tests/test_ingestion_integration.py, which runs the same chain against a
real statement PDF. The HSBC parser consumes plain text (PDF extraction
happens upstream), so the whole pipeline can be exercised without any real
statement data. All names and amounts below are fictional.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from parsers.hsbc import parse_with_balances, reconcile
from persistence.database import init_schema
from persistence.repositories import (
    AccountRepository, CategoryRepository, VendorRuleRepository,
    TransactionRepository, ReviewQueueRepository, ExchangeRateRepository,
    SnapshotRepository,
)
from services.ingestion import ingest_transactions
from services.reconciliation import record_snapshot

HEADER = "Date Payment type and details £ Paid out £ Paid in £ Balance"
FOOTER = "Information about the Financial Services"


def make_statement(body: str) -> str:
    return f"Your Statement\n{HEADER}\n{body}\n{FOOTER}\n"


# One month: salary credit (matches a rule), rent and utility debits,
# a multi-line card purchase, a card refund (sign-corrected by the balance
# checkpoint), and a same-day repeat purchase. Closing balance ties out.
STATEMENT = make_statement("""01 Jun 25 BALANCE BROUGHT FORWARD 1,000.00
02 Jun 25 DD CITY LETTINGS RENT 950.00 50.00
03 Jun 25 CR ACME CORP 2,800.00 2,850.00
04 Jun 25 DD CITY POWER CO 62.40 2,787.60
05 Jun 25 VIS CORNER GROCER
TOWNSVILLE 43.75 2,743.85
06 Jun 25 VIS TRANSIT TOPUP 11.10
VIS TRANSIT TOPUP 11.10 2,721.65
07 Jun 25 VIS HOMEWARE STORE 30.00 2,751.65
30 Jun 25 BALANCE CARRIED FORWARD 2,751.65""")

EXPECTED_TXNS = 7


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_schema(c)
    c.execute("INSERT INTO owners (name) VALUES ('Demo User')")
    c.execute("""
        INSERT INTO accounts (account_code, display_name, account_type, currency, owner_id)
        VALUES ('UK_CURRENT', 'UK Current Account', 'checking', 'GBP', 1)
    """)
    c.execute("INSERT INTO categories (name, money_type) VALUES ('Miscellaneous', 'expense')")
    c.execute("INSERT INTO categories (name, money_type) VALUES ('Income - Salary', 'income')")
    c.execute("INSERT INTO categories (name, money_type) VALUES ('Utilities - Energy', 'expense')")
    c.execute("""
        INSERT INTO vendor_rules (pattern, match_type, category_id, money_type, confidence)
        SELECT 'ACME CORP', 'contains', id, 'income', 'high' FROM categories WHERE name='Income - Salary'
    """)
    c.execute("""
        INSERT INTO vendor_rules (pattern, match_type, category_id, money_type, confidence)
        SELECT 'CITY POWER CO', 'contains', id, 'expense', 'high' FROM categories WHERE name='Utilities - Energy'
    """)
    c.commit()
    return c


def _run_pipeline(conn, text, statement_name):
    txns, opening, closing = parse_with_balances(text)
    recon = reconcile(txns, opening_balance=opening, closing_balance=closing)
    account_id = AccountRepository(conn).get_id_by_code("UK_CURRENT")
    inserted, skipped = ingest_transactions(
        txns, account_id, statement_name,
        TransactionRepository(conn), VendorRuleRepository(conn), CategoryRepository(conn),
        ReviewQueueRepository(conn), ExchangeRateRepository(conn),
    )
    record_snapshot(SnapshotRepository(conn), account_id, "2025-06", recon)
    conn.commit()
    return txns, recon, inserted, skipped, account_id


def test_statement_flows_through_whole_pipeline(conn):
    txns, recon, inserted, skipped, account_id = _run_pipeline(conn, STATEMENT, "2025-06.pdf")

    # Parser + parser-level reconciliation
    assert len(txns) == EXPECTED_TXNS
    assert recon.is_clean, "synthetic statement must tie out exactly"

    # Ingestion: every parsed row persisted, none mistaken for a duplicate
    # (the two same-day TRANSIT TOPUPs are both real)
    assert skipped == 0
    assert len(inserted) == EXPECTED_TXNS
    assert TransactionRepository(conn).count_all() == EXPECTED_TXNS
    topups = conn.execute("SELECT COUNT(*) FROM transactions WHERE description='TRANSIT TOPUP'").fetchone()[0]
    assert topups == 2

    # Sum of stored amounts equals the statement's movement, to the penny
    total = conn.execute("SELECT ROUND(SUM(settlement_amount), 2) FROM transactions").fetchone()[0]
    assert total == pytest.approx(2751.65 - 1000.00, abs=0.001)


def test_signs_survive_into_the_database(conn):
    _run_pipeline(conn, STATEMENT, "2025-06.pdf")
    amount = lambda desc: conn.execute(
        "SELECT settlement_amount FROM transactions WHERE description=?", (desc,)).fetchone()[0]
    assert amount("CITY LETTINGS RENT") == pytest.approx(-950.00)  # expense negative
    assert amount("ACME CORP") == pytest.approx(2800.00)           # credit positive
    assert amount("HOMEWARE STORE") == pytest.approx(30.00)        # VIS refund flipped by checkpoint
    assert amount("CORNER GROCER TOWNSVILLE") == pytest.approx(-43.75)  # multi-line description joined


def test_rules_categorise_and_everything_else_goes_to_review(conn):
    _run_pipeline(conn, STATEMENT, "2025-06.pdf")

    salary = conn.execute(
        "SELECT t.money_type, t.confidence, c.name FROM transactions t "
        "JOIN categories c ON c.id = t.category_id WHERE t.description='ACME CORP'").fetchone()
    assert (salary["money_type"], salary["confidence"], salary["name"]) == ("income", "high", "Income - Salary")

    energy = conn.execute(
        "SELECT c.name FROM transactions t JOIN categories c ON c.id = t.category_id "
        "WHERE t.description='CITY POWER CO'").fetchone()
    assert energy["name"] == "Utilities - Energy"

    # 7 rows, 2 matched by high-confidence rules -> 5 wait for a human
    assert ReviewQueueRepository(conn).count_open() == EXPECTED_TXNS - 2


def test_snapshot_records_clean_reconciliation(conn):
    _, _, _, _, account_id = _run_pipeline(conn, STATEMENT, "2025-06.pdf")
    snap = AccountRepository(conn).latest_snapshot(account_id)
    assert snap["year_month"] == "2025-06"
    assert snap["reconciled"] == 1
    assert snap["reconciliation_diff"] == pytest.approx(0.0, abs=0.001)
    assert snap["statement_closing_balance"] == pytest.approx(2751.65)


def test_gap_is_carried_through_to_the_snapshot_not_absorbed(conn):
    """Honest reconciliation end to end: a statement whose printed closing
    balance doesn't match its transactions is still ingested, but the
    snapshot records the gap instead of pretending it tied out."""
    gappy = make_statement("""01 Jun 25 BALANCE BROUGHT FORWARD 100.00
02 Jun 25 DD CITY POWER CO 20.00 80.00
30 Jun 25 BALANCE CARRIED FORWARD 75.00""")
    _, recon, inserted, _, account_id = _run_pipeline(conn, gappy, "2025-06-gap.pdf")

    assert not recon.is_clean
    assert len(inserted) == 1
    snap = AccountRepository(conn).latest_snapshot(account_id)
    assert snap["reconciled"] == 0
    assert abs(snap["reconciliation_diff"]) == pytest.approx(5.00, abs=0.001)


def test_reimporting_the_same_statement_is_rejected(conn):
    _run_pipeline(conn, STATEMENT, "2025-06.pdf")
    with pytest.raises(ValueError):
        _run_pipeline(conn, STATEMENT, "2025-06.pdf")
    assert TransactionRepository(conn).count_all() == EXPECTED_TXNS
