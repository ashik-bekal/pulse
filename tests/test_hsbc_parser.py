"""
HSBC parser tests against synthetic statement text.

The parser consumes plain text (PDF extraction happens upstream), so these
tests exercise the full block-segmentation pipeline — type codes, multi-line
descriptions, balance checkpoints, sign correction, FX breakouts — without
any statement PDFs.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from parsers.hsbc import parse_with_balances, reconcile

HEADER = "Date Payment type and details £ Paid out £ Paid in £ Balance"
FOOTER = "Information about the Financial Services"


def make_statement(body: str) -> str:
    return f"Your Statement\n{HEADER}\n{body}\n{FOOTER}\n"


BASIC = make_statement("""01 Jun 25 BALANCE BROUGHT FORWARD 1,000.00
02 Jun 25 DD ACME UTILITIES 50.00 950.00
03 Jun 25 VIS COFFEE SHOP
TOWNSVILLE 10.00 940.00
04 Jun 25 CR EMPLOYER PAYROLL 500.00 1,440.00
06 Jun 25 BALANCE CARRIED FORWARD 1,440.00""")


def test_parses_opening_and_closing_balances():
    _, opening, closing = parse_with_balances(BASIC)
    assert opening == 1000.00
    assert closing == 1440.00


def test_parses_transactions_with_multiline_descriptions():
    txns, _, _ = parse_with_balances(BASIC)
    assert len(txns) == 3
    dd, vis, cr = txns
    assert dd.description == "ACME UTILITIES"
    assert dd.settlement_amount == -50.00
    assert vis.description == "COFFEE SHOP TOWNSVILLE"
    assert vis.settlement_amount == -10.00
    assert vis.date == "2025-06-03"
    assert cr.settlement_amount == 500.00  # CR type code = credit


def test_exact_reconciliation_ties_out():
    txns, opening, closing = parse_with_balances(BASIC)
    result = reconcile(txns, opening_balance=opening, closing_balance=closing)
    assert result.is_clean
    assert result.diff == pytest.approx(0.0)


def test_sign_correction_flips_card_refund_to_credit():
    """VIS lines default to debit, but a refund moves the balance UP —
    the checkpoint-based sign correction must flip it."""
    text = make_statement("""01 Jun 25 BALANCE BROUGHT FORWARD 100.00
02 Jun 25 VIS SHOP REFUND 40.00 140.00
03 Jun 25 BALANCE CARRIED FORWARD 140.00""")
    txns, opening, closing = parse_with_balances(text)
    assert txns[0].settlement_amount == 40.00  # flipped positive
    result = reconcile(txns, opening_balance=opening, closing_balance=closing)
    assert result.is_clean


def test_reconciliation_gap_is_reported_not_swallowed():
    text = make_statement("""01 Jun 25 BALANCE BROUGHT FORWARD 100.00
02 Jun 25 DD ACME UTILITIES 50.00 50.00
03 Jun 25 BALANCE CARRIED FORWARD 75.00""")
    txns, opening, closing = parse_with_balances(text)
    result = reconcile(txns, opening_balance=opening, closing_balance=closing)
    assert not result.is_clean
    assert result.diff == pytest.approx(-25.00)


def test_account_summary_opening_fallback():
    """First statement of a new account has no BALANCE BROUGHT FORWARD —
    the Account Summary box's OpeningBalance is used instead."""
    text = f"""Your Statement
Account Summary
OpeningBalance £0.00
{HEADER}
02 Jun 25 CR FIRST DEPOSIT 250.00 250.00
03 Jun 25 BALANCE CARRIED FORWARD 250.00
{FOOTER}
"""
    txns, opening, closing = parse_with_balances(text)
    assert opening == 0.00
    assert closing == 250.00
    assert reconcile(txns, opening_balance=opening, closing_balance=closing).is_clean


def test_fx_breakout_produces_native_and_settled_amounts():
    text = make_statement("""01 Jun 25 BALANCE BROUGHT FORWARD 100.00
02 Jun 25 VIS EURO CAFE
EUR 45.00 @ 1.1250
Visa Rate 40.00
60.00
03 Jun 25 BALANCE CARRIED FORWARD 60.00""")
    txns, opening, closing = parse_with_balances(text)
    fx = txns[0]
    assert fx.transaction_currency == "EUR"
    assert fx.transaction_amount == -45.00
    assert fx.settlement_currency == "GBP"
    assert fx.settlement_amount == -40.00
    assert fx.fx_rate == pytest.approx(1.1250)
    assert reconcile(txns, opening_balance=opening, closing_balance=closing).is_clean


def test_missing_opening_balance_skips_reconciliation_gracefully():
    text = make_statement("""02 Jun 25 DD ACME UTILITIES 50.00 950.00""")
    txns, opening, closing = parse_with_balances(text)
    assert opening is None
    result = reconcile(txns, opening_balance=opening, closing_balance=closing)
    assert result.diff is None
    assert result.mismatches  # carries the "skipped" note
