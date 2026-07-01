"""
Chase checking/savings parser tests against synthetic statement text.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from parsers.chase_bank import parse_per_account, reconcile

COMBINED = """CHASE TOTAL CHECKING
Account Number: 000000000011
Beginning Balance $1,000.00
05/01 Employer Payroll 500.00 1,500.00
05/03 Grocery Store -50.00 1,450.00
Ending Balance $1,450.00
CHASE SAVINGS
Account Number: 000000000022
Beginning Balance $200.00
05/10 Interest Payment 0.10 200.10
Ending Balance $200.10
"""


def test_splits_checking_and_savings_sections():
    accounts = parse_per_account(COMBINED, statement_year=2025, statement_start_month=5)
    assert set(accounts) == {"Chase Total Checking", "Chase Savings"}
    checking = accounts["Chase Total Checking"]
    assert checking["beginning_balance"] == 1000.00
    assert checking["ending_balance"] == 1450.00
    assert len(checking["transactions"]) == 2
    assert checking["transactions"][0].settlement_amount == 500.00
    assert checking["transactions"][1].settlement_amount == -50.00
    assert checking["transactions"][0].date == "2025-05-01"


def test_each_section_reconciles_against_running_balances():
    accounts = parse_per_account(COMBINED, statement_year=2025, statement_start_month=5)
    for data in accounts.values():
        result = reconcile(
            data["transactions"],
            beginning_balance=data["beginning_balance"],
            ending_balance=data["ending_balance"],
        )
        assert result.diff == pytest.approx(0.0)
        assert not result.mismatches


def test_marketing_mention_does_not_create_phantom_account():
    """Boilerplate like '...personal Chase savings accounts...' matches the
    section-header regex but never accumulates balances or transactions —
    it must be dropped, not passed to reconcile() where None balances crash."""
    text = """Important update about your Chase Total Checking Monthly Service Fee
Qualifying deposits include personal Chase savings accounts (excluding premium tiers)
CHASE TOTAL CHECKING
Account Number: 000000000011
Beginning Balance $100.00
06/01 Coffee -3.00 97.00
Ending Balance $97.00
"""
    accounts = parse_per_account(text, statement_year=2025, statement_start_month=6)
    assert list(accounts) == ["Chase Total Checking"]


def test_year_rollover_only_at_december_boundary():
    """A Dec-start statement rolls January into the next year, but a
    mid-year statement must not bump earlier-month REBILL lines a year
    forward."""
    text = """CHASE TOTAL CHECKING
Beginning Balance $100.00
12/30 Late December -10.00 90.00
01/02 Early January -5.00 85.00
Ending Balance $85.00
"""
    accounts = parse_per_account(text, statement_year=2025, statement_start_month=12)
    txns = accounts["Chase Total Checking"]["transactions"]
    assert txns[0].date == "2025-12-30"
    assert txns[1].date == "2026-01-02"
