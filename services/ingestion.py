"""
Ingestion service.

PREVIOUS DESIGN PROBLEM: scripts/ingest_statement.py was ~300 lines mixing
PDF text extraction, per-format parsing dispatch, currency-pair
normalization (now handled inside each parser itself), categorization,
database writes, reconciliation reporting, and snapshot recording, all
inline inside three large ingest_hsbc/ingest_chase_checking_savings/
ingest_sapphire functions. Nothing in that file was unit-testable without
a real SQLite file and a real PDF on disk.

FIX: this module exposes one function per account TYPE (not per format
quirk) that takes already-parsed RawTransaction lists plus already-loaded
repositories, and does exactly three things: categorize, persist, flag for
review. PDF text extraction and parsing stay in parsers/ (called by cli/ or
web/, not here) — this service doesn't know what a PDF is. Reconciliation
math stays in parsers/ + services/reconciliation.py. This service is the
thin layer that wires categorization + persistence together, which is the
one piece that previously had no name and lived as inline code.
"""
from typing import List, Optional

from domain.categorization import categorize
from domain.models import RawTransaction
from persistence.repositories import (
    TransactionRepository, VendorRuleRepository, CategoryRepository,
    ReviewQueueRepository, ExchangeRateRepository,
)
from services.fx import to_reporting_currency

REVIEW_CONFIDENCE_LEVELS = ("low", "medium")


def ingest_transactions(
    transactions: List[RawTransaction],
    account_id: int,
    source_statement: str,
    txn_repo: TransactionRepository,
    rule_repo: VendorRuleRepository,
    category_repo: CategoryRepository,
    review_repo: ReviewQueueRepository,
    rate_repo: ExchangeRateRepository,
    is_credit_card: bool = False,
) -> List[int]:
    """
    Categorize and persist a batch of already-parsed transactions for one
    account. Returns the list of inserted transaction IDs.

    Loads vendor rules ONCE for the whole batch (fixing the N+1 query that
    existed when categorization opened its own connection per transaction),
    then categorizes and inserts each transaction, flagging low/medium
    confidence results into the review queue exactly as the original
    insert_transaction() helper did.
    """
    if txn_repo.already_imported(account_id, source_statement):
        raise ValueError(f"Already imported: {source_statement!r} for account {account_id}. Delete the existing transactions first if you want to re-import.")

    # Sort once for the whole batch: categorize() requires descending pattern
    # length so that more-specific rules win, but sorting inside categorize()
    # itself means O(R log R) per transaction. Pre-sorting here reduces it to
    # O(R log R) once + O(R) per transaction.
    rules = sorted(rule_repo.list_active(), key=lambda r: len(r.pattern), reverse=True)
    fallback_category_id = category_repo.get_id_by_name("Miscellaneous")

    # Snapshot taken ONCE before any inserts in this batch — see
    # existing_fingerprint_counts() docstring for why this must not be a
    # live per-row lookup (it would falsely flag legitimate same-day repeat
    # transactions, like two identical transit top-ups, as duplicates of
    # each other).
    existing_counts = txn_repo.existing_fingerprint_counts(account_id)

    inserted_ids = []
    skipped = 0
    for txn in transactions:
        fingerprint = (txn.date, txn.description, round(txn.settlement_amount, 2))
        if existing_counts.get(fingerprint, 0) > 0:
            existing_counts[fingerprint] -= 1
            skipped += 1
            continue

        decision = categorize(txn.description, rules, fallback_category_id=fallback_category_id,
                             _presorted=True, amount=txn.settlement_amount, is_credit_card=is_credit_card)
        year_month = txn.date[:7]
        reporting_amount = to_reporting_currency(
            rate_repo, txn.settlement_amount, txn.settlement_currency, year_month
        )

        txn_id = txn_repo.insert(account_id, txn, decision, reporting_amount, source_statement)
        inserted_ids.append(txn_id)

        if decision.confidence in REVIEW_CONFIDENCE_LEVELS:
            issue = (
                f"Auto-categorized with {decision.confidence} confidence"
                + (f" (matched: {decision.matched_pattern})" if decision.matched_pattern
                   else " (no vendor rule matched)")
            )
            review_repo.add(txn_id, issue, decision.confidence)

    return inserted_ids, skipped


def flag_fx_sanity_failures(transactions: List[RawTransaction], inserted_ids: List[int],
                             failures: List[dict], review_repo: ReviewQueueRepository) -> None:
    """
    Separately surfaces FX sanity-check failures (native * rate != billed
    amount) into the review queue at HIGH severity — these indicate a
    parsing problem, not a categorization-confidence problem, so they're
    recorded independently of the low/medium-confidence flagging above.
    Only the Sapphire parser currently produces these.
    """
    if not failures:
        return
    failure_dates_descriptions = {(f["date"], f["description"]) for f in failures}
    for txn, txn_id in zip(transactions, inserted_ids):
        if (txn.date, txn.description) in failure_dates_descriptions:
            review_repo.add(
                txn_id,
                f"FX sanity check failed: native*rate vs billed amount mismatch "
                f"(native={txn.transaction_amount} {txn.transaction_currency}, rate={txn.fx_rate})",
                "high",
            )
