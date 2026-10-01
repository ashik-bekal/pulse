"""Tests for parser-independent running-balance reconciliation rules."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from domain.models import RawTransaction
from domain.reconciliation import reconcile_running_balance


def transaction(amount: float, balance_after: float) -> RawTransaction:
    return RawTransaction(
        date="2026-07-01",
        description="Synthetic transaction",
        transaction_currency="GBP",
        transaction_amount=amount,
        settlement_currency="GBP",
        settlement_amount=amount,
        balance_after=balance_after,
        raw_source_lines=["synthetic source line"],
    )


def test_clean_running_balance_and_summary_totals():
    transactions = [transaction(-10.00, 90.00), transaction(25.00, 115.00)]
    result = reconcile_running_balance(
        transactions,
        opening_balance=100.00,
        closing_balance=115.00,
        stated_money_out=10.00,
        stated_money_in=25.00,
    )
    assert result.is_clean
    assert result.computed_balance == 115.00


def test_running_balance_checkpoint_mismatch_is_reported():
    result = reconcile_running_balance(
        [transaction(-10.00, 95.00)],
        opening_balance=100.00,
        closing_balance=90.00,
    )
    assert not result.is_clean
    assert result.mismatches[0]["computed_balance"] == 90.00
    assert result.mismatches[0]["stated_balance"] == 95.00


def test_summary_total_mismatch_is_reported():
    result = reconcile_running_balance(
        [transaction(-10.00, 90.00)],
        opening_balance=100.00,
        closing_balance=90.00,
        stated_money_out=12.00,
        stated_money_in=0.00,
    )
    assert not result.is_clean
    assert result.mismatches == [{
        "note": "Parsed money out does not match statement summary",
        "parsed": 10.00,
        "stated": 12.00,
    }]


def test_missing_opening_balance_is_explicitly_unverifiable():
    result = reconcile_running_balance(
        [transaction(-10.00, 90.00)],
        opening_balance=None,
        closing_balance=90.00,
    )
    assert not result.is_clean
    assert result.computed_balance is None
    assert result.diff is None
    assert result.mismatches
