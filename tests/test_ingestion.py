"""
Ingestion service tests on an in-memory database: duplicate detection
semantics, sign-based money-type fallback, and review-queue flagging.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from domain.models import RawTransaction
from persistence.database import init_schema
from persistence.repositories import (
    AccountRepository, CategoryRepository, VendorRuleRepository,
    TransactionRepository, ReviewQueueRepository, ExchangeRateRepository,
)
from services.ingestion import ingest_transactions


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
    c.commit()
    return c


def repos(c):
    return dict(
        txn_repo=TransactionRepository(c), rule_repo=VendorRuleRepository(c),
        category_repo=CategoryRepository(c), review_repo=ReviewQueueRepository(c),
        rate_repo=ExchangeRateRepository(c),
    )


def txn(date="2025-06-02", desc="COFFEE SHOP", amount=-4.50):
    return RawTransaction(
        date=date, description=desc,
        transaction_currency="GBP", transaction_amount=amount,
        settlement_currency="GBP", settlement_amount=amount,
        raw_source_lines=[f"{date} {desc} {amount}"],
    )


def ingest(c, txns, statement="stmt-1.pdf", **kwargs):
    r = repos(c)
    account_id = AccountRepository(c).get_id_by_code("UK_CURRENT")
    return ingest_transactions(
        txns, account_id, statement,
        r["txn_repo"], r["rule_repo"], r["category_repo"], r["review_repo"], r["rate_repo"],
        **kwargs,
    )


def test_same_day_repeat_transactions_all_insert(conn):
    """Two identical transit top-ups on one day are both real — within-batch
    repeats must never shadow each other as duplicates."""
    batch = [txn(desc="TRANSIT TOPUP", amount=-11.10), txn(desc="TRANSIT TOPUP", amount=-11.10)]
    inserted, skipped = ingest(conn, batch)
    assert len(inserted) == 2
    assert skipped == 0


def test_reimport_of_overlapping_statement_skips_existing(conn):
    ingest(conn, [txn()], statement="june.pdf")
    conn.commit()
    # Same transaction arrives again in a different (overlapping) statement
    inserted, skipped = ingest(conn, [txn(), txn(desc="NEW SHOP")], statement="june-july.pdf")
    assert len(inserted) == 1
    assert skipped == 1


def test_exact_refile_of_same_statement_is_rejected(conn):
    ingest(conn, [txn()], statement="june.pdf")
    conn.commit()
    with pytest.raises(ValueError):
        ingest(conn, [txn()], statement="june.pdf")


def test_unmatched_credit_defaults_to_income_on_bank_account(conn):
    inserted, _ = ingest(conn, [txn(desc="MYSTERY CREDIT", amount=250.0)])
    row = conn.execute("SELECT money_type FROM transactions WHERE id=?", (inserted[0],)).fetchone()
    assert row["money_type"] == "income"


def test_unmatched_credit_defaults_to_reimbursement_on_credit_card(conn):
    """A card refund is not income — it reverses an expense."""
    inserted, _ = ingest(conn, [txn(desc="SHOP REFUND", amount=30.0)], is_credit_card=True)
    row = conn.execute("SELECT money_type FROM transactions WHERE id=?", (inserted[0],)).fetchone()
    assert row["money_type"] == "reimbursement"


def test_unmatched_transactions_land_in_review_queue(conn):
    ingest(conn, [txn()])
    conn.commit()
    assert ReviewQueueRepository(conn).count_open() == 1
