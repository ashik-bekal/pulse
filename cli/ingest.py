"""
CLI for ingesting bank statements into the ledger.

Thin entry point: does PDF text extraction (the one I/O concern unique to
"ingesting a file from disk") and calls into parsers/ and services/ for
everything else.

Usage:
    python3 cli/ingest.py hsbc /path/to/statement.pdf
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pdfplumber

from persistence.database import get_connection
from persistence.repositories import (
    AccountRepository, CategoryRepository, VendorRuleRepository, TransactionRepository,
    ReviewQueueRepository, ExchangeRateRepository, SnapshotRepository,
)
from parsers import hsbc
from services.ingestion import ingest_transactions
from services.reconciliation import record_snapshot, format_report

ACCOUNT_UK_CURRENT = "UK_CURRENT"


def _extract_text(pdf_path: str) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join(p.extract_text() for p in pdf.pages if p.extract_text())


def ingest_hsbc(conn, pdf_path: str, target_account_id: int = None):
    text = _extract_text(pdf_path)
    transactions, opening, closing = hsbc.parse_with_balances(text)

    account_id = target_account_id or AccountRepository(conn).get_id_by_code(ACCOUNT_UK_CURRENT)

    # If neither BALANCE BROUGHT FORWARD nor Account Summary opening found,
    # fall back to the account-level opening balance set by the user.
    if opening is None:
        row = conn.execute(
            "SELECT opening_balance_native FROM accounts WHERE id=?",
            (account_id,)
        ).fetchone()
        if row and row["opening_balance_native"] is not None:
            opening = row["opening_balance_native"]

    result = hsbc.reconcile(transactions, opening_balance=opening, closing_balance=closing)
    inserted_ids, skipped = ingest_transactions(
        transactions, account_id, os.path.basename(pdf_path),
        TransactionRepository(conn), VendorRuleRepository(conn),
        CategoryRepository(conn), ReviewQueueRepository(conn), ExchangeRateRepository(conn),
    )

    print(f"\n=== HSBC UK: {os.path.basename(pdf_path)} ===")
    if opening is None:
        print("  ⚠ Opening balance not found — reconciliation skipped (transactions still imported)")
    print(f"Opening: {opening}  Closing: {closing}")
    print(f"Transactions parsed: {len(transactions)}  Inserted: {len(inserted_ids)}  Skipped (duplicates): {skipped}")
    print(format_report("HSBC UK", result))

    year_month = transactions[-1].date[:7] if transactions else None
    if year_month:
        record_snapshot(SnapshotRepository(conn), account_id, year_month, result)
    return inserted_ids, skipped, result.is_clean, result.diff


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("account_type", choices=["hsbc"])
    parser.add_argument("pdf_path")
    args = parser.parse_args()

    conn = get_connection()
    if args.account_type == "hsbc":
        ingest_hsbc(conn, args.pdf_path)

    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
