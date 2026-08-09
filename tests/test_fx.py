"""
services/fx.py: exact-rate conversion, the nearest-prior/nearest-later
fallback, the no-rate-at-all NULL case, and that ingestion surfaces every
fallback/missing case to the review queue at high severity.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from domain.models import RawTransaction  # noqa: E402
from persistence.database import init_schema  # noqa: E402
from persistence.repositories import (  # noqa: E402
    CategoryRepository, ExchangeRateRepository, ReviewQueueRepository,
    TransactionRepository, VendorRuleRepository,
)
from services.fx import to_reporting_currency  # noqa: E402
from services.ingestion import ingest_transactions  # noqa: E402


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_schema(c)
    c.execute("INSERT INTO owners (name) VALUES ('Demo User')")
    c.execute("""
        INSERT INTO accounts (account_code, display_name, account_type, currency, owner_id)
        VALUES ('US_CHECKING', 'US Checking', 'checking', 'USD', 1)
    """)
    c.execute("INSERT INTO categories (name, money_type) VALUES ('Miscellaneous', 'expense')")
    c.commit()
    return c


@pytest.fixture
def rate_repo(conn):
    return ExchangeRateRepository(conn)


def test_exact_rate(conn, rate_repo):
    rate_repo.upsert("2025-06", "USD", 0.74)
    conn.commit()
    result = to_reporting_currency(rate_repo, -100.0, "USD", "2025-06")
    assert result[0] == pytest.approx(-74.0)
    assert result[1] is None


def test_gbp_identity(rate_repo):
    assert to_reporting_currency(rate_repo, 50.0, "GBP", "2025-06") == (50.0, None)


def test_prior_month_fallback(conn, rate_repo):
    rate_repo.upsert("2025-04", "USD", 0.75)
    rate_repo.upsert("2025-05", "USD", 0.76)
    conn.commit()
    amount, note = to_reporting_currency(rate_repo, 100.0, "USD", "2025-07")
    assert amount == pytest.approx(76.0)
    assert "2025-05" in note


def test_later_month_fallback(conn, rate_repo):
    rate_repo.upsert("2025-09", "USD", 0.80)
    conn.commit()
    amount, note = to_reporting_currency(rate_repo, 100.0, "USD", "2025-06")
    assert amount == pytest.approx(80.0)
    assert "2025-09" in note


def test_no_rate_at_all(rate_repo):
    amount, note = to_reporting_currency(rate_repo, 100.0, "USD", "2025-06")
    assert amount is None
    assert note


def test_ingestion_flags_fallback(conn, rate_repo):
    ExchangeRateRepository(conn).upsert("2025-05", "USD", 0.75)
    conn.commit()

    txn_repo = TransactionRepository(conn)
    review_repo = ReviewQueueRepository(conn)
    txn = RawTransaction(
        date="2025-06-02", description="COFFEE SHOP",
        transaction_currency="USD", transaction_amount=-5.0,
        settlement_currency="USD", settlement_amount=-5.0,
    )
    ingest_transactions(
        [txn], account_id=1, source_statement="test.pdf",
        txn_repo=txn_repo, rule_repo=VendorRuleRepository(conn),
        category_repo=CategoryRepository(conn), review_repo=review_repo,
        rate_repo=rate_repo,
    )
    rows = conn.execute(
        "SELECT * FROM review_queue WHERE severity='high' AND issue_description LIKE 'FX rate issue:%'"
    ).fetchall()
    assert len(rows) == 1
