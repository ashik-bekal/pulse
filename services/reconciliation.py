"""
Reconciliation service.

Wraps the per-format reconcile() functions (which live in parsers/, since
each format's reconciliation math is specific to that format) with the
generic "record a snapshot and produce a human-readable report" behavior
that's identical across all three account types. This is what the original
ingest_statement.py duplicated three times (once per ingest_* function).
"""
from domain.models import ReconciliationResult
from persistence.repositories import SnapshotRepository


def record_snapshot(snapshot_repo: SnapshotRepository, account_id: int, year_month: str,
                     result: ReconciliationResult) -> None:
    # When opening balance wasn't found, computed_balance is None but stated_balance
    # (from "BALANCE CARRIED FORWARD") is still available — use it so the dashboard
    # can show the account balance even without a full reconciliation.
    closing = result.computed_balance if result.computed_balance is not None else result.stated_balance
    if closing is None:
        return  # nothing to record — neither balance was extractable
    snapshot_repo.upsert(
        account_id=account_id,
        year_month=year_month,
        closing_balance_native=closing,
        statement_closing_balance=result.stated_balance,
        reconciled=result.is_clean,
        reconciliation_diff=result.diff,
    )


def format_report(account_label: str, result: ReconciliationResult) -> str:
    lines = [
        f"Reconciliation ({account_label}): computed={result.computed_balance} "
        f"stated={result.stated_balance} diff={result.diff}"
    ]
    if result.mismatches:
        lines.append(f"  *** {len(result.mismatches)} BALANCE MISMATCHES - DO NOT TRUST THIS BATCH ***")
        for m in result.mismatches:
            lines.append(f"    {m}")
    elif result.is_clean:
        lines.append("  Reconciliation: EXACT TIE-OUT, no mismatches.")
    else:
        lines.append(f"  *** RECONCILIATION GAP: {result.diff} ***")
    return "\n".join(lines)
