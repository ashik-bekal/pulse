"""
Chase credit card parser tests against synthetic statement text, covering
the layout quirks discovered against real statements: doubled-character
section headers, three-line FX breakouts, sub-dollar amounts printed
without a leading zero, and negative (credit) statement balances.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from parsers.chase_sapphire import (
    parse, extract_balances, reconcile, fx_sanity_failures, _normalize_doubled_chars,
)

STATEMENT = """Previous Balance $100.00
New Balance $55.36
Opening/Closing Date 05/11/25 - 06/10/25
AACCCCOOUUNNTT AACCTTIIVVIITTYY
PAYMENTS AND OTHER CREDITS
05/22 Payment Thank You-Mobile -100.00
PURCHASE
05/23 COFFEE PLACE CITY 12.50
05/24 TINY SNACK .27
05/25 FOREIGN BISTRO LONDON 13.59
05/26 POUND STERLING
10.00 X 1.359000000 (EXCHG RATE)
FEES CHARGED
06/09 LATE FEE 29.00
2025 Totals Year-to-Date
"""


def test_normalizes_doubled_character_headers():
    assert _normalize_doubled_chars("AACCCCOOUUNNTT AACCTTIIVVIITTYY") == "ACCOUNT ACTIVITY"


def test_parses_all_line_shapes():
    txns = parse(STATEMENT, statement_year=2025, statement_start_month=5)
    by_desc = {t.description: t for t in txns}
    assert len(txns) == 5

    # Payment (negative on statement) becomes a positive credit
    assert by_desc["Payment Thank You-Mobile"].settlement_amount == 100.00
    # Ordinary purchase
    assert by_desc["COFFEE PLACE CITY"].settlement_amount == -12.50
    # Sub-dollar amount printed as ".27" with no leading zero
    assert by_desc["TINY SNACK"].settlement_amount == pytest.approx(-0.27)
    # Fee line under FEES CHARGED
    assert by_desc["LATE FEE"].settlement_amount == -29.00


def test_fx_three_line_breakout():
    txns = parse(STATEMENT, statement_year=2025, statement_start_month=5)
    fx = next(t for t in txns if t.fx_rate is not None)
    assert fx.description == "FOREIGN BISTRO LONDON"
    assert fx.transaction_currency == "GBP"
    assert fx.transaction_amount == -10.00
    assert fx.settlement_currency == "USD"
    assert fx.settlement_amount == pytest.approx(-13.59)
    assert fx_sanity_failures(txns) == []


def test_reconciles_against_account_summary():
    txns = parse(STATEMENT, statement_year=2025, statement_start_month=5)
    prev, new = extract_balances(STATEMENT)
    assert (prev, new) == (100.00, 55.36)
    result = reconcile(txns, previous_balance=prev, new_balance=new)
    assert result.diff == pytest.approx(0.0)


def test_negative_new_balance_extracts():
    """A card in credit prints 'New Balance -$3.66' (sign before the $)."""
    text = "Previous Balance $6.25\nNew Balance -$3.66\n"
    prev, new = extract_balances(text)
    assert prev == 6.25
    assert new == pytest.approx(-3.66)


def test_fx_sanity_flags_mismatched_rate():
    text = """Previous Balance $0.00
New Balance $50.00
ACCOUNT ACTIVITY
PURCHASE
05/25 SUSPICIOUS SHOP LONDON 50.00
05/26 POUND STERLING
10.00 X 1.359000000 (EXCHG RATE)
2025 Totals Year-to-Date
"""
    txns = parse(text, statement_year=2025, statement_start_month=5)
    failures = fx_sanity_failures(txns)
    assert len(failures) == 1  # 10 * 1.359 = 13.59, nowhere near 50.00
