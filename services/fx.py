"""
Currency conversion service.

Isolated from the ingestion pipeline because "convert settlement_amount to
reporting currency for a given month" is a distinct concern from "parse a
PDF" or "categorize a transaction" — and isolating it means a future
correctness fix touches exactly one file instead of being buried inline in
an ingestion script.

Fallback policy: an exact month/currency rate is preferred. If missing, the
nearest PRIOR month's stored rate is used (a currency's rate barely moves
month to month, and "the last rate you entered" is what you'd have used
anyway); if no prior month exists, the nearest LATER month's rate is used.
If the currency has no stored rate at all, reporting_amount is left NULL
rather than guessing. Every fallback or NULL is surfaced to the review
queue — never silent.
"""
from persistence.repositories import ExchangeRateRepository


def to_reporting_currency(rate_repo: ExchangeRateRepository, amount: float,
                           currency: str, year_month: str):
    """Returns (reporting_amount_or_None, fallback_note_or_None).
    fallback_note is a human-readable string when a non-exact rate (or no rate)
    was used; callers must surface it to the review queue."""
    rate, fallback_month = rate_repo.get_rate_or_fallback(year_month, currency)
    if rate is None:
        return None, (f"No exchange rate exists for {currency} (any month); "
                      f"reporting amount left empty — add a rate on the Rates page, "
                      f"then re-import or edit this transaction.")
    if fallback_month is not None:
        return amount * rate, (f"No {currency} rate for {year_month}; used {fallback_month} "
                               f"rate {rate} instead — verify on the Rates page.")
    return amount * rate, None
