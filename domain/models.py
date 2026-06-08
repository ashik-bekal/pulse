"""
Domain models — the canonical, parser-agnostic shapes used everywhere else
in the app.

This module has ZERO dependencies on I/O, databases, or specific statement
formats. That's deliberate: it's the one part of the codebase every other
layer can safely import without pulling in SQLite, Flask, or pdfplumber.

Why a single RawTransaction shape:
Previously each parser (HSBC, Chase, Sapphire) returned its own dataclass
with different field names for the same concept (paid_out/paid_in vs signed
amount vs usd_amount). The ingestion layer then had per-account branches to
normalize these differences before writing to the database. That branching
is deleted by having every parser emit this one shape instead. The shape
itself encodes the two-currency-pair model established during parser
development: transaction_currency/amount is what currency the purchase
actually happened in; settlement_currency/amount is what hit the account
balance. For domestic transactions these are identical.
"""
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass(frozen=True)
class RawTransaction:
    """
    Canonical output of every statement parser. Frozen (immutable) because
    parser output should never be mutated in place — any enrichment
    (categorization, trip tagging) produces a new object downstream.
    """
    date: str                          # YYYY-MM-DD, the TRANSACTION date (not posting date)
    description: str
    transaction_currency: str
    transaction_amount: float          # signed: negative = outflow, positive = inflow
    settlement_currency: str
    settlement_amount: float           # signed, same convention, in the account's own currency
    fx_rate: Optional[float] = None
    balance_after: Optional[float] = None   # statement-printed running balance, for reconciliation only
    raw_source_lines: List[str] = field(default_factory=list)  # for audit/debugging traceability


@dataclass(frozen=True)
class ReconciliationResult:
    """
    Result of tying parsed transactions against a statement's own printed
    balances. `mismatches` being non-empty means: do not trust this batch.
    """
    computed_balance: float
    stated_balance: Optional[float]
    diff: Optional[float]           # None when stated_balance is unavailable
    mismatches: List[dict] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        if self.diff is None:
            return not self.mismatches
        return abs(self.diff) < 0.01 and not self.mismatches


@dataclass(frozen=True)
class CategorizationDecision:
    """Output of the categorization engine for a single transaction."""
    category_id: Optional[int]
    money_type: Optional[str]
    owner_id: Optional[int]
    confidence: str
    matched_rule_id: Optional[int]
    matched_pattern: Optional[str]


@dataclass(frozen=True)
class VendorRule:
    """
    A single categorization rule. Plain data — the matching algorithm lives
    in domain/categorization.py, not on this class, to keep this a pure
    value object.
    """
    id: int
    pattern: str
    match_type: str            # 'contains' | 'exact' | 'regex'
    category_id: Optional[int]
    money_type: Optional[str]
    owner_id: Optional[int]
    confidence: str
    is_active: bool = True
