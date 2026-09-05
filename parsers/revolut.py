"""Parser for Revolut current-account statements exported as plain text.

Revolut statements contain an account-level balance summary followed by rows
with a transaction amount and a printed running balance. PDF extraction drops
the empty ``Money out`` or ``Money in`` cell, so direction is recovered from
the movement in the running balance rather than merchant-name heuristics.
"""
import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from domain.models import RawTransaction
from domain.reconciliation import PENNY_TOLERANCE, reconcile_running_balance
from parsers.base import (
    ParseContext, ParsedAccount, ParsedStatement, StatementDetection, parse_money,
)


FORMAT_NAME = "revolut"
_AMOUNT = r"[-+]?[£$€]?[\d,]+\.\d{2}"
_SUMMARY_RE = re.compile(
    rf"^Account\s+\((?P<label>[^)]+)\)\s+"
    rf"(?P<opening>{_AMOUNT})\s+(?P<money_out>{_AMOUNT})\s+"
    rf"(?P<money_in>{_AMOUNT})\s+(?P<closing>{_AMOUNT})\s*$",
    re.IGNORECASE,
)
_TRANSACTION_RE = re.compile(
    rf"^(?P<day>\d{{1,2}})\s+(?P<month>[A-Za-z]{{3}})\s+"
    rf"(?P<year>\d{{4}})\s+(?P<description>.+?)(?:\s+|(?=[-+]?[£$€]))"
    rf"(?P<amount>{_AMOUNT})\s+(?P<balance>{_AMOUNT})\s*$"
)
_CURRENCY_RE = re.compile(r"^([A-Z]{3}) Statement\s*$", re.MULTILINE)
_PERIOD_RE = re.compile(
    r"Account transactions from\s+\d{1,2}\s+([A-Za-z]+)\s+(\d{4})\b",
    re.IGNORECASE,
)
_DETAIL_PREFIXES = ("To:", "From:", "Card:", "Reference:")


@dataclass
class _TransactionRow:
    date: str
    description: str
    amount: float
    balance_after: float
    raw_source_lines: List[str]


def _iso_date(day: str, month: str, year: str) -> str:
    return datetime.strptime(f"{day} {month} {year}", "%d %b %Y").date().isoformat()


def _matches_format(text: str) -> bool:
    """Require branding plus statement structure, not a brand mention alone."""
    return all((
        re.search(r"\bRevolut Bank\b", text, re.IGNORECASE),
        re.search(r"\bBalance summary\b", text, re.IGNORECASE),
        re.search(r"\bAccount transactions from\b", text, re.IGNORECASE),
    ))


def _extract_detection_metadata(text: str) -> StatementDetection:
    period_match = _PERIOD_RE.search(text)
    year = start_month = None
    period_label = None
    if period_match:
        month_name, year_text = period_match.groups()
        for date_format in ("%B %Y", "%b %Y"):
            try:
                period = datetime.strptime(f"{month_name} {year_text}", date_format)
                year = period.year
                start_month = period.month
                period_label = period.strftime("%b %Y")
                break
            except ValueError:
                continue

    return StatementDetection(
        format_name=FORMAT_NAME,
        account_label="Revolut",
        year=year,
        start_month=start_month,
        period_label=period_label,
        confidence="high" if period_label else "medium",
        notes="Detected Revolut statement." + (
            f" Period: {period_label}." if period_label else ""
        ),
        last4=None,
    )


def detect(text: str) -> Optional[StatementDetection]:
    """Return Revolut statement metadata, or ``None`` for another format."""
    if not _matches_format(text):
        return None
    return _extract_detection_metadata(text)


def _extract_rows(text: str) -> List[_TransactionRow]:
    rows: List[_TransactionRow] = []
    current: Optional[_TransactionRow] = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        match = _TRANSACTION_RE.match(line)
        if match:
            current = _TransactionRow(
                date=_iso_date(match.group("day"), match.group("month"), match.group("year")),
                description=match.group("description").strip(),
                amount=abs(parse_money(match.group("amount"))),
                balance_after=parse_money(match.group("balance")),
                raw_source_lines=[line],
            )
            rows.append(current)
            continue

        # Detail lines can cross a PDF page boundary. Repeated page and table
        # headers are deliberately ignored while the current transaction stays
        # active, allowing details on the next page to remain attributable.
        if current is not None and line.startswith(_DETAIL_PREFIXES):
            current.raw_source_lines.append(line)

    return rows


def _signed_transactions(
    rows: List[_TransactionRow], opening_balance: Optional[float], currency: str,
) -> List[RawTransaction]:
    transactions: List[RawTransaction] = []
    previous_balance = opening_balance

    for row in rows:
        amount = row.amount
        if previous_balance is not None:
            debit_balance = previous_balance - amount
            credit_balance = previous_balance + amount
            if abs(credit_balance - row.balance_after) <= PENNY_TOLERANCE:
                amount = amount
            elif abs(debit_balance - row.balance_after) <= PENNY_TOLERANCE:
                amount = -amount
            else:
                # An unverifiable row must not be silently treated as income.
                # Defaulting to outflow is conservative; reconciliation records
                # the checkpoint mismatch and prevents a clean result.
                amount = -amount
        else:
            amount = -amount

        transactions.append(RawTransaction(
            date=row.date,
            description=row.description,
            transaction_currency=currency,
            transaction_amount=amount,
            settlement_currency=currency,
            settlement_amount=amount,
            balance_after=row.balance_after,
            raw_source_lines=list(row.raw_source_lines),
        ))
        previous_balance = row.balance_after

    return transactions


def parse(text: str, *, context: ParseContext) -> ParsedStatement:
    """Parse and reconcile one Revolut account statement."""
    del context  # Revolut transaction rows contain complete dates.

    warnings: List[str] = []
    currency_match = _CURRENCY_RE.search(text)
    currency = currency_match.group(1) if currency_match else "GBP"
    if currency_match is None:
        warnings.append("Statement currency not found; defaulted to GBP")

    summary_match = next(
        (match for line in text.splitlines()
         if (match := _SUMMARY_RE.match(line.strip()))),
        None,
    )
    if summary_match:
        label = summary_match.group("label").strip()
        opening_balance = parse_money(summary_match.group("opening"))
        stated_money_out = parse_money(summary_match.group("money_out"))
        stated_money_in = parse_money(summary_match.group("money_in"))
        closing_balance = parse_money(summary_match.group("closing"))
    else:
        label = "Current Account"
        opening_balance = stated_money_out = stated_money_in = closing_balance = None
        warnings.append("Account balance summary not found")

    rows = _extract_rows(text)
    if not rows:
        warnings.append("No transaction rows found")
    transactions = _signed_transactions(rows, opening_balance, currency)
    print(transactions)
    reconciliation = reconcile_running_balance(
        transactions,
        opening_balance,
        closing_balance,
        stated_money_out,
        stated_money_in,
    )

    return ParsedStatement(
        format_name=FORMAT_NAME,
        accounts=[ParsedAccount(
            label=label,
            account_number=None,
            transactions=transactions,
            reconciliation=reconciliation,
        )],
        warnings=warnings,
    )
