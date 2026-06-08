"""
Currency conversion service.

Isolated from the ingestion pipeline because "convert settlement_amount to
reporting currency for a given month" is a distinct concern from "parse a
PDF" or "categorize a transaction" — and isolating it means a future
correctness fix touches exactly one file instead of being buried inline in
an ingestion script.

BEHAVIOR PRESERVED FROM ORIGINAL: a missing exchange rate for a given
month/currency combination prints a warning and falls back to a 1.0 rate,
rather than raising. This is almost certainly the wrong failure mode for a
money app — silently treating $100 as £100 is a real correctness risk —
but changing it is a behavior change outside the scope of "architecture
only, no behavior changes." Flagging this explicitly rather than fixing it
quietly: this should become a hard failure (raise, surface to the
ingestion report) in a deliberate follow-up change, not slipped in here.
"""
from persistence.repositories import ExchangeRateRepository


def to_reporting_currency(rate_repo: ExchangeRateRepository, amount: float,
                           currency: str, year_month: str) -> float:
    """
    Converts a settlement-currency amount to the reporting currency (GBP)
    using that month's rate.
    """
    try:
        rate = rate_repo.get_rate(year_month, currency)
    except ValueError:
        print(f"  WARNING: no exchange rate found for {currency} in {year_month}, "
              f"using 1.0 (WRONG - fix exchange_rates table)")
        rate = 1.0
    return amount * rate
