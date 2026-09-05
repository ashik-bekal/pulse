"""Revolut parser tests using synthetic statement text."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from parsers.base import ParseContext
from parsers.revolut import detect, parse


STATEMENT = """GBP Statement
Generated on the 31 Aug 2026
Revolut Bank UK Ltd
Balance summary
Closing
Product Opening balance Money out Money in
balance
Account (Current Account) £100.00 £15.00 £50.00 £135.00
Total £100.00 £15.00 £50.00 £135.00
Account transactions from 1 July 2026 to 31 July 2026
Date Description Money out Money in Balance
1 Jul 2026 Example Parking £10.00 £90.00
To: Example Parking Ltd, Testville
Card: ****************
15 Jul 2026 Employer payment £50.00 £140.00

GBP Statement
Generated on the 31 Aug 2026
Revolut Bank UK Ltd
Date Description Money out Money in Balance
Reference: JULY PAY
31 Jul 2026 Example Transit £5.00 £135.00
"""


def parse_statement(text: str = STATEMENT):
    return parse(
        text,
        context=ParseContext(statement_year=2026, statement_start_month=7),
    )


def test_detects_format_and_extracts_period_metadata():
    detection = detect(STATEMENT)
    assert detection is not None
    assert detection.format_name == "revolut"
    assert detection.account_label == "Revolut"
    assert detection.year == 2026
    assert detection.start_month == 7
    assert detection.period_label == "Jul 2026"
    assert detection.confidence == "high"
    assert detection.last4 is None


def test_detection_requires_brand_and_structural_markers():
    assert detect("Revolut Bank UK Ltd mentioned in passing") is None
    assert detect("Balance summary\nAccount transactions from 1 July 2026") is None


def test_returns_normalized_single_account_statement():
    statement = parse_statement()
    assert statement.format_name == "revolut"
    assert statement.warnings == []
    assert len(statement.accounts) == 1
    assert statement.accounts[0].label == "Current Account"
    assert statement.accounts[0].account_number is None


def test_parses_debits_credit_dates_and_running_balances():
    transactions = parse_statement().accounts[0].transactions
    assert len(transactions) == 3

    parking, salary, ringgo = transactions
    assert parking.date == "2026-07-01"
    assert parking.description == "Example Parking"
    assert parking.settlement_currency == "GBP"
    assert parking.transaction_amount == -10.00
    assert parking.settlement_amount == -10.00
    assert parking.balance_after == 90.00

    assert salary.description == "Employer payment"
    assert salary.transaction_amount == 50.00
    assert salary.settlement_amount == 50.00
    assert ringgo.settlement_amount == -5.00


def test_preserves_detail_lines_across_repeated_page_headers():
    transactions = parse_statement().accounts[0].transactions
    assert transactions[0].raw_source_lines == [
        "1 Jul 2026 Example Parking £10.00 £90.00",
        "To: Example Parking Ltd, Testville",
        "Card: ****************",
    ]
    assert "Reference: JULY PAY" in transactions[1].raw_source_lines
    assert all("GBP Statement" not in line for line in transactions[1].raw_source_lines)


def test_parses_transaction_when_currency_symbol_touches_description():
    text = """GBP Statement
Revolut Bank UK Ltd
Balance summary
Account (Current Account) £200.00 £130.00 £0.00 £70.00
Account transactions from 1 July 2026 to 31 July 2026
Date Description Money out Money in Balance
30 Jul 2026 Transfer to Synthetic Recipient REF123£130.00 £70.00
Reference: Synthetic transfer
To: Synthetic Recipient
"""
    transactions = parse_statement(text).accounts[0].transactions

    assert len(transactions) == 1
    assert transactions[0].description == "Transfer to Synthetic Recipient REF123"
    assert transactions[0].settlement_amount == -130.00
    assert transactions[0].balance_after == 70.00
    assert transactions[0].raw_source_lines == [
        "30 Jul 2026 Transfer to Synthetic Recipient REF123£130.00 £70.00",
        "Reference: Synthetic transfer",
        "To: Synthetic Recipient",
    ]


def test_reconciles_to_summary_and_printed_checkpoints():
    reconciliation = parse_statement().accounts[0].reconciliation
    assert reconciliation.is_clean
    assert reconciliation.computed_balance == 135.00
    assert reconciliation.stated_balance == 135.00
    assert reconciliation.diff == pytest.approx(0.0)
    assert reconciliation.mismatches == []


def test_missing_summary_is_explicitly_unverifiable():
    text = """GBP Statement
Date Description Money out Money in Balance
1 Jul 2026 Coffee £4.00 £96.00
"""
    statement = parse_statement(text)
    account = statement.accounts[0]
    assert "Account balance summary not found" in statement.warnings
    assert not account.reconciliation.is_clean
    assert account.reconciliation.diff is None
    assert account.reconciliation.mismatches
