"""
Shared parser infrastructure.

New statement-format parsers implement one statement-oriented entry point:
`parse(text, context=...) -> ParsedStatement`. A parsed statement contains
one or more independently reconciled accounts. This accommodates both
single-account statements and combined statements without making callers
know format-specific balance fields or reconciliation arguments.

The older parsers are migrated to this interface incrementally. There's no
abstract base class enforcing it through inheritance — Python's structural
typing (a Protocol) is enough and avoids introducing stateful parser objects.

This module holds the genuinely shared pieces: money-string parsing, ISO
date conversion, and a Protocol definition for static-typing tools to check
against.
"""
import datetime
import re
from dataclasses import dataclass, field
from typing import List, Optional, Protocol, runtime_checkable

from domain.models import RawTransaction, ReconciliationResult


SUPPORTED_STATEMENT_FORMATS = ("hsbc", "chase_bank", "sapphire", "revolut")


@dataclass(frozen=True)
class ParseContext:
    """Statement metadata supplied by detection when the text lacks it."""

    statement_year: Optional[int] = None
    statement_start_month: Optional[int] = None


@dataclass(frozen=True)
class StatementDetection:
    """Normalized format and statement metadata returned by parser detection."""

    format_name: str
    account_label: str
    year: Optional[int]
    start_month: Optional[int]
    period_label: Optional[str]
    confidence: str
    notes: str
    last4: Optional[str] = None


@dataclass(frozen=True)
class ParsedAccount:
    """One account extracted and reconciled independently from a statement."""

    label: str
    account_number: Optional[str]
    transactions: List[RawTransaction]
    reconciliation: ReconciliationResult


@dataclass(frozen=True)
class ParsedStatement:
    """Normalized output shared by every statement-format parser."""

    format_name: str
    accounts: List[ParsedAccount]
    warnings: List[str] = field(default_factory=list)

# Matches "1,234.56" but NOT "1.1544" (a 4+ decimal FX rate) — the
# negative lookahead for a following digit prevents matching a 2-decimal
# substring out of a longer decimal number. This fixed a real bug during
# parser development where "1.1544" was incorrectly read as "1.15".
MONEY_RE = re.compile(r"[\d,]+\.\d{2}(?!\d)")

_CURRENCY_SYMBOLS = str.maketrans("", "", "$£€")


def parse_money(s: str) -> float:
    return float(s.replace(",", "").translate(_CURRENCY_SYMBOLS))


def to_iso_date(mm: str, dd: str, statement_year: int, statement_start_month: int) -> str:
    """
    Convert MM/DD to YYYY-MM-DD.

    A statement's billing window spans exactly two adjacent calendar months
    (its start month and the following one — e.g. 10 and 11), so the ONLY
    case where the second month rolls into the next calendar year is when
    start_month is December and the transaction's month is January.

    Any OTHER month smaller than start_month is NOT a year-boundary wrap —
    it's most often a late "REBILL"/adjustment line (seen on Chase Sapphire
    statements for merchants like Booking.com) whose date field is the
    ORIGINAL transaction's date, which can be months in the past. The old
    "month < start_month => next year" rule pushed those into next year
    (e.g. an August transaction on an October statement became next
    August), which is wrong far more often than it's right. Keeping the
    statement's own year for any month that isn't the genuine wrap case is
    the safer default.
    """
    month = int(mm)
    month_after_start = statement_start_month % 12 + 1
    if month == month_after_start and month_after_start < statement_start_month:
        year = statement_year + 1
    else:
        year = statement_year
    return f"{year:04d}-{month:02d}-{int(dd):02d}"


def iso_date(day: str, month: str, year: str) -> str:
    return datetime.strptime(f"{day} {month} {year}", "%d %b %Y").date().isoformat()


@runtime_checkable
class StatementParser(Protocol):
    def detect(self, text: str) -> Optional[StatementDetection]: ...
    def parse(self, text: str, *, context: ParseContext) -> ParsedStatement: ...
