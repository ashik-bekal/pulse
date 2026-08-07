"""
Shared parser infrastructure: MM/DD -> ISO date year-wrap logic, and
money-string parsing (including the FX-rate lookahead guard on MONEY_RE).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from parsers.base import to_iso_date, parse_money, MONEY_RE  # noqa: E402


def test_normal_month():
    assert to_iso_date("10", "05", 2025, 10) == "2025-10-05"


def test_month_after_start_same_year():
    assert to_iso_date("11", "03", 2025, 10) == "2025-11-03"


def test_december_to_january_wraps_year():
    assert to_iso_date("01", "15", 2025, 12) == "2026-01-15"


def test_rebill_old_month_keeps_statement_year():
    # NOT 2026 — this is the REBILL rule: a month below start_month that
    # isn't the genuine December->January wrap keeps the statement's year.
    assert to_iso_date("08", "20", 2025, 10) == "2025-08-20"


def test_january_on_january_statement_not_wrapped():
    assert to_iso_date("01", "10", 2025, 1) == "2025-01-10"


def test_parse_money():
    assert parse_money("1,234.56") == 1234.56
    assert parse_money("£12.00") == 12.0
    assert parse_money("$0.99") == 0.99
    assert parse_money("€1,000.00") == 1000.0


def test_money_re_ignores_fx_rates():
    assert MONEY_RE.findall("Exchange rate 1.1544 charged 26.00") == ["26.00"]
