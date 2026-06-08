"""
Categorization engine — pure function, no I/O.

PREVIOUS DESIGN PROBLEM: the original categorizer.categorize() opened its
own sqlite3 connection and ran a fresh query against vendor_rules on every
single call. With N transactions in a batch, that's N database round-trips
for what is fundamentally a pure computation (match a string against a list
of patterns). It also meant the categorizer could never be unit-tested
without a real database file on disk.

FIX: categorize() now takes the list of rules as a plain argument. The
caller (a service in services/) is responsible for loading rules from the
database ONCE per batch and passing them in. This turns an O(N) query
pattern into O(1), and makes the function trivially testable with a
hand-built list of VendorRule objects.

Matching strategy unchanged from the original: rules are evaluated in
DESCENDING pattern length, so a more specific override (e.g. "ACME CORP
CAFETERIA") always wins over a shorter generic pattern (e.g. "ACME
CORP") regardless of which was defined first. This correctly resolves
name clashes where an employer's payroll credit and a same-named venue
charge need opposite classifications.
"""
import re
from typing import List, Optional

from domain.models import VendorRule, CategorizationDecision

# Caller may not have a "Miscellaneous" category id handy; this sentinel
# lets the caller decide how to map an unmatched transaction, keeping this
# module free of any assumption about category IDs.
UNMATCHED_CONFIDENCE = "low"


def categorize(description: str, rules: List[VendorRule],
               fallback_category_id: Optional[int] = None,
               _presorted: bool = False,
               amount: Optional[float] = None,
               is_credit_card: bool = False) -> CategorizationDecision:
    """
    Find the best-matching active rule for a transaction description.
    "Best" = longest matching pattern (most specific).

    Pass _presorted=True when the caller has already sorted rules by
    descending pattern length (e.g. ingest_transactions pre-sorts once per
    batch). When False (the default), sorting happens here so ad-hoc callers
    still get correct results without thinking about order.

    `amount` (the transaction's settlement_amount, positive = money in,
    negative = money out) is only used when NO vendor rule matches — an
    explicit rule's money_type always wins, since a human already decided
    that pattern's classification. Without a match, the sign of the amount
    is a far better guess than the previous hardcoded "expense" default,
    which mis-tagged every unmatched credit (refund, salary, etc.) as a
    spend and pushed it into the review queue misleadingly pre-set to "Exp".

    `is_credit_card` distinguishes a positive amount on a liability account
    (a refund/credit against a purchase — "reimbursement") from a positive
    amount on an asset account (money actually arriving — "income"). Tagging
    a card refund as income would double-count it: the money never left your
    checking account balance in the first place, it just un-does an expense.
    """
    desc_upper = description.upper()

    candidates = [r for r in rules if r.is_active]
    if not _presorted:
        candidates.sort(key=lambda r: len(r.pattern), reverse=True)

    for rule in candidates:
        if _matches(rule, desc_upper, description):
            return CategorizationDecision(
                category_id=rule.category_id,
                money_type=rule.money_type,
                owner_id=rule.owner_id,
                confidence=rule.confidence,
                matched_rule_id=rule.id,
                matched_pattern=rule.pattern,
            )

    if amount is not None and amount > 0:
        fallback_money_type = "reimbursement" if is_credit_card else "income"
    else:
        fallback_money_type = "expense"
    return CategorizationDecision(
        category_id=fallback_category_id,
        money_type=fallback_money_type,
        owner_id=None,
        confidence=UNMATCHED_CONFIDENCE,
        matched_rule_id=None,
        matched_pattern=None,
    )


def _matches(rule: VendorRule, desc_upper: str, desc_original: str) -> bool:
    pattern_upper = rule.pattern.upper()
    if rule.match_type == "contains":
        return pattern_upper in desc_upper
    if rule.match_type == "exact":
        return pattern_upper == desc_upper
    if rule.match_type == "regex":
        return bool(re.search(rule.pattern, desc_original, re.IGNORECASE))
    raise ValueError(f"Unknown match_type: {rule.match_type}")
