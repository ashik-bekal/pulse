"""Pure reconciliation rules shared by statement parsers."""
from typing import List, Optional

from domain.models import RawTransaction, ReconciliationResult


PENNY_TOLERANCE = 0.01


def reconcile_running_balance(
    transactions: List[RawTransaction],
    opening_balance: Optional[float],
    closing_balance: Optional[float],
    stated_money_out: Optional[float] = None,
    stated_money_in: Optional[float] = None,
) -> ReconciliationResult:
    """Reconcile signed transactions against statement balance information.

    ``stated_money_out`` and ``stated_money_in`` are optional because not all
    statement formats print aggregate movement totals. When supplied, they
    provide an additional completeness check alongside running and closing
    balances.
    """
    if opening_balance is None:
        return ReconciliationResult(
            computed_balance=None,
            stated_balance=closing_balance,
            diff=None,
            mismatches=[{"note": "Opening balance not found — reconciliation skipped"}],
        )

    balance = opening_balance
    mismatches = []
    for transaction in transactions:
        balance += transaction.settlement_amount
        if (transaction.balance_after is not None
                and abs(balance - transaction.balance_after) > PENNY_TOLERANCE):
            mismatches.append({
                "date": transaction.date,
                "description": transaction.description,
                "computed_balance": round(balance, 2),
                "stated_balance": transaction.balance_after,
                "diff": round(balance - transaction.balance_after, 2),
            })
        # Resume from a printed checkpoint so one malformed or omitted row
        # does not produce a misleading cascade of later mismatches.
        if transaction.balance_after is not None:
            balance = transaction.balance_after

    parsed_money_out = round(
        -sum(min(transaction.settlement_amount, 0.0) for transaction in transactions),
        2,
    )
    parsed_money_in = round(
        sum(max(transaction.settlement_amount, 0.0) for transaction in transactions),
        2,
    )
    if (stated_money_out is not None
            and abs(parsed_money_out - stated_money_out) > PENNY_TOLERANCE):
        mismatches.append({
            "note": "Parsed money out does not match statement summary",
            "parsed": parsed_money_out,
            "stated": stated_money_out,
        })
    if (stated_money_in is not None
            and abs(parsed_money_in - stated_money_in) > PENNY_TOLERANCE):
        mismatches.append({
            "note": "Parsed money in does not match statement summary",
            "parsed": parsed_money_in,
            "stated": stated_money_in,
        })

    computed_balance = round(
        opening_balance + sum(transaction.settlement_amount for transaction in transactions),
        2,
    )
    diff = round(computed_balance - closing_balance, 2) if closing_balance is not None else None
    return ReconciliationResult(
        computed_balance=computed_balance,
        stated_balance=closing_balance,
        diff=diff,
        mismatches=mismatches,
    )
