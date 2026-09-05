"""
CLI for ingesting bank/card statements into the ledger.

This is now a THIN entry point: it does PDF text extraction (the one I/O
concern unique to "ingesting a file from disk") and calls into parsers/ and
services/ for everything else. Compare to the original scripts/
ingest_statement.py, where parsing, categorization, persistence, and
reconciliation reporting were all written inline per account type.

Usage:
    python3 cli/ingest.py hsbc /path/to/statement.pdf
    python3 cli/ingest.py chase_bank /path/to/statement.pdf --year 2026 --start-month 4
    python3 cli/ingest.py sapphire /path/to/statement.pdf --year 2026 --start-month 4
"""
import argparse
import os
import sys
from typing import Mapping, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pdfplumber

from persistence.database import get_connection
from persistence.repositories import (
    AccountRepository, CategoryRepository, VendorRuleRepository, TransactionRepository,
    ReviewQueueRepository, ExchangeRateRepository, SnapshotRepository,
)
from parsers import hsbc, chase_bank, chase_sapphire, revolut
from parsers.base import ParseContext
from services.ingestion import ingest_transactions, flag_fx_sanity_failures
from services.reconciliation import record_snapshot, format_report

ACCOUNT_UK_CURRENT = "UK_CURRENT"
ACCOUNT_US_CHECKING = "US_CHECKING"
ACCOUNT_US_SAVINGS = "US_SAVINGS"
ACCOUNT_US_CREDIT_CARD = "US_CREDIT_CARD"

STATEMENT_PARSERS = {
    "revolut": revolut,
}


def _extract_text(pdf_path: str) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join(p.extract_text() for p in pdf.pages if p.extract_text())


def ingest_statement(
    conn,
    pdf_path: str,
    filetype: str,
    target_account_id: Optional[int] = None,
    target_account_ids: Optional[Mapping[str, int]] = None,
    context: Optional[ParseContext] = None,
):
    """Ingest every account returned by a conforming statement parser.

    ``target_account_id`` is convenient for a single-account statement.
    Multi-account statements use ``target_account_ids``, keyed by the parsed
    account number where available, or otherwise by its label.
    """
    try:
        statement_parser = STATEMENT_PARSERS[filetype]
    except KeyError as exc:
        raise ValueError(f"Unknown statement filetype: {filetype}") from exc

    text = _extract_text(pdf_path)
    parsed_statement = statement_parser.parse(text, context=context or ParseContext())
    if not parsed_statement.accounts:
        raise ValueError(f"{filetype} parser returned no accounts")
    if target_account_id is not None and target_account_ids is not None:
        raise ValueError("Supply either target_account_id or target_account_ids, not both")
    if len(parsed_statement.accounts) > 1 and target_account_id is not None:
        raise ValueError(
            "target_account_id can only be used for a single-account statement; "
            "use target_account_ids for multiple accounts"
        )

    resolved_accounts = []
    for parsed_account in parsed_statement.accounts:
        ledger_account_id = target_account_id
        if target_account_ids is not None:
            lookup_keys = [parsed_account.account_number, parsed_account.label]
            ledger_account_id = next(
                (target_account_ids[key] for key in lookup_keys
                 if key is not None and key in target_account_ids),
                None,
            )
        if ledger_account_id is None:
            identifier = parsed_account.account_number or parsed_account.label
            raise ValueError(f"No target ledger account supplied for {identifier!r}")
        resolved_accounts.append((parsed_account, ledger_account_id))

    transaction_repo = TransactionRepository(conn)
    vendor_rule_repo = VendorRuleRepository(conn)
    category_repo = CategoryRepository(conn)
    review_repo = ReviewQueueRepository(conn)
    exchange_rate_repo = ExchangeRateRepository(conn)
    snapshot_repo = SnapshotRepository(conn)
    source_statement = os.path.basename(pdf_path)

    for warning in parsed_statement.warnings:
        print(f"  ⚠ {warning}")

    all_inserted_ids = []
    total_skipped = 0
    reconciliation_results = []
    for parsed_account, ledger_account_id in resolved_accounts:
        inserted_ids, skipped = ingest_transactions(
            parsed_account.transactions,
            ledger_account_id,
            source_statement,
            transaction_repo,
            vendor_rule_repo,
            category_repo,
            review_repo,
            exchange_rate_repo,
        )
        all_inserted_ids.extend(inserted_ids)
        total_skipped += skipped
        reconciliation_results.append(parsed_account.reconciliation)

        print(f"\n=== {parsed_account.label}: {source_statement} ===")
        print(
            f"Transactions parsed: {len(parsed_account.transactions)}  "
            f"Inserted: {len(inserted_ids)}  Skipped (duplicates): {skipped}"
        )
        print(format_report(parsed_account.label, parsed_account.reconciliation))

        if parsed_account.transactions:
            year_month = parsed_account.transactions[-1].date[:7]
            record_snapshot(
                snapshot_repo,
                ledger_account_id,
                year_month,
                parsed_account.reconciliation,
            )

    all_reconciled = all(result.is_clean for result in reconciliation_results)
    if all_reconciled:
        reconciliation_diff = 0.0
    elif len(reconciliation_results) == 1:
        reconciliation_diff = reconciliation_results[0].diff
    elif any(result.diff is None for result in reconciliation_results):
        reconciliation_diff = None
    else:
        reconciliation_diff = max(abs(result.diff) for result in reconciliation_results)
    return all_inserted_ids, total_skipped, all_reconciled, reconciliation_diff


def ingest_revolut(conn, pdf_path: str, target_account_id: int):
    return ingest_statement(
        conn,
        pdf_path,
        "revolut",
        target_account_id=target_account_id,
    )


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


def ingest_chase_bank(conn, pdf_path: str, year: int, start_month: int, target_account_id: int = None):
    text = _extract_text(pdf_path)
    accounts = chase_bank.parse_per_account(text, statement_year=year, statement_start_month=start_month)

    # A Chase Checking/Savings PDF can contain a "Chase Total Checking"
    # section, a "Chase Savings" section, or both — but the labels
    # themselves don't say WHOSE account this is, only which product type.
    # When a target account is given (because more than one account shares
    # this format, e.g. two people's separate Chase Checking logins), route
    # only the section matching that account's own type to it; any other
    # section in the same PDF still resolves via the default hardcoded
    # account for that type.
    target_account_type = None
    if target_account_id:
        row = conn.execute("SELECT account_type FROM accounts WHERE id=?", (target_account_id,)).fetchone()
        target_account_type = row["account_type"] if row else None

    print(f"\n=== Chase Checking/Savings: {os.path.basename(pdf_path)} ===")
    account_repo = AccountRepository(conn)
    all_inserted = []
    total_skipped = 0
    all_reconciled = True
    max_diff = 0.0
    for label, acct_data in accounts.items():
        label_account_type = "checking" if "Checking" in label else "savings"
        if target_account_id and label_account_type == target_account_type:
            account_id = target_account_id
        else:
            account_code = ACCOUNT_US_CHECKING if "Checking" in label else ACCOUNT_US_SAVINGS
            account_id = account_repo.get_id_by_code(account_code)

        result = chase_bank.reconcile(
            acct_data["transactions"], beginning_balance=acct_data["beginning_balance"],
            ending_balance=acct_data["ending_balance"],
        )
        inserted_ids, skipped = ingest_transactions(
            acct_data["transactions"], account_id, os.path.basename(pdf_path),
            TransactionRepository(conn), VendorRuleRepository(conn),
            CategoryRepository(conn), ReviewQueueRepository(conn), ExchangeRateRepository(conn),
        )
        all_inserted.extend(inserted_ids)
        total_skipped += skipped
        all_reconciled = all_reconciled and result.is_clean
        max_diff = max(max_diff, abs(result.diff or 0.0))

        print(f"\n  -- {label} --")
        print(f"  Begin: {acct_data['beginning_balance']}  End: {acct_data['ending_balance']}")
        print(f"  Transactions: {len(acct_data['transactions'])}  Inserted: {len(inserted_ids)}  Skipped (duplicates): {skipped}")
        print("  " + format_report(label, result).replace("\n", "\n  "))

        year_month = acct_data["transactions"][-1].date[:7] if acct_data["transactions"] else f"{year:04d}-{start_month:02d}"
        record_snapshot(SnapshotRepository(conn), account_id, year_month, result)
    return all_inserted, total_skipped, all_reconciled, (max_diff if not all_reconciled else 0.0)


def ingest_sapphire(conn, pdf_path: str, year: int, start_month: int, target_account_id: int = None):
    text = _extract_text(pdf_path)
    transactions = chase_sapphire.parse(text, statement_year=year, statement_start_month=start_month)
    previous_balance, new_balance = chase_sapphire.extract_balances(text)
    result = chase_sapphire.reconcile(transactions, previous_balance=previous_balance, new_balance=new_balance)
    failures = chase_sapphire.fx_sanity_failures(transactions)

    account_id = target_account_id or AccountRepository(conn).get_id_by_code(ACCOUNT_US_CREDIT_CARD)
    review_repo = ReviewQueueRepository(conn)
    inserted_ids, skipped = ingest_transactions(
        transactions, account_id, os.path.basename(pdf_path),
        TransactionRepository(conn), VendorRuleRepository(conn),
        CategoryRepository(conn), review_repo, ExchangeRateRepository(conn),
        is_credit_card=True,
    )
    flag_fx_sanity_failures(transactions, inserted_ids, failures, review_repo)

    print(f"\n=== Chase Sapphire Reserve: {os.path.basename(pdf_path)} ===")
    print(f"Previous balance: {previous_balance}  New balance: {new_balance}")
    print(f"Transactions parsed: {len(transactions)}  Inserted: {len(inserted_ids)}  Skipped (duplicates): {skipped}")
    if failures:
        print(f"  *** {len(failures)} FX SANITY CHECK FAILURES - REVIEW BEFORE TRUSTING ***")
        for f in failures:
            print(f"    {f}")
    print(format_report("Chase Sapphire Reserve", result))

    year_month = transactions[-1].date[:7] if transactions else f"{year:04d}-{start_month:02d}"
    record_snapshot(SnapshotRepository(conn), account_id, year_month, result)
    return inserted_ids, skipped, result.is_clean, result.diff


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("account_type", choices=["hsbc", "chase_bank", "sapphire"])
    parser.add_argument("pdf_path")
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--start-month", type=int, default=None)
    args = parser.parse_args()

    conn = get_connection()
    if args.account_type == "hsbc":
        ingest_hsbc(conn, args.pdf_path)
    elif args.account_type == "chase_bank":
        ingest_chase_bank(conn, args.pdf_path, args.year, args.start_month)
    elif args.account_type == "sapphire":
        ingest_sapphire(conn, args.pdf_path, args.year, args.start_month)

    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
