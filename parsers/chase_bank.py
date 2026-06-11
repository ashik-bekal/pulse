"""
Chase Total Checking / Chase Savings statement parser.

Algorithm unchanged from the validated version (zero reconciliation diff
against real statements). Output shape changed: emits canonical
RawTransaction grouped by account label, instead of a bespoke
ParsedTransaction with a signed `amount` field that the ingestion layer
had to massage into the two-currency-pair model used everywhere else.
Since this account type is purely domestic USD, transaction_currency
always equals settlement_currency (both "USD") and there's no FX rate.

Layout notes preserved verbatim:
- Statement PDF contains BOTH Checking and Savings sections, delimited by
  CHASE TOTAL CHECKING / CHASE SAVINGS headers and *start*transactiondetail
  ... *end*transactiondetail markers.
- One line per transaction: "MM/DD Description -1,200.00 8,294.21"
  Last token = running balance, second-to-last = signed amount.
- A same-day-posted line sometimes duplicates MM/DD at the start of its own
  description (e.g. "05/12 05/12 Payment To Chase Card..."); the duplicate
  is stripped.
"""
import re
from typing import Dict, List, Optional

from domain.models import RawTransaction, ReconciliationResult
from parsers.base import parse_money, to_iso_date

DATE_RE = re.compile(r"^(\d{2})/(\d{2})\b")
MONEY_RE = re.compile(r"-?[\d,]+\.\d{2}")
ACCOUNT_HEADER_RE = re.compile(r"(CHASE TOTAL CHECKING|CHASE SAVINGS)", re.IGNORECASE)
ACCOUNT_NUMBER_RE = re.compile(r"Account Number:\s*([\d]+)")
BEGIN_BAL_RE = re.compile(r"Beginning Balance\s+\$?([\d,]+\.\d{2})")
END_BAL_RE = re.compile(r"Ending Balance\s+\$?([\d,]+\.\d{2})")


def parse(text: str, statement_year: int, statement_start_month: int,
          **_context) -> List[RawTransaction]:
    """
    Parse a combined Chase Checking+Savings statement.

    statement_year / statement_start_month: year and month of the
    statement period START (e.g. "April 22, 2026" -> 2026, 4).

    Unlike the HSBC/Sapphire parsers (one account per call), this returns
    transactions for BOTH accounts found in the PDF in one list. Each
    RawTransaction doesn't carry an account identifier — the caller (an
    ingestion service) is expected to call parse_per_account() instead if
    it needs the accounts kept separate, which is the common case since
    each account reconciles independently.
    """
    accounts = parse_per_account(text, statement_year, statement_start_month)
    all_txns = []
    for acct_data in accounts.values():
        all_txns.extend(acct_data["transactions"])
    return all_txns


def parse_per_account(text: str, statement_year: int, statement_start_month: int) -> Dict[str, dict]:
    """
    Returns:
        {
            "Chase Total Checking": {
                "account_number": "...",
                "beginning_balance": float,
                "ending_balance": float,
                "transactions": List[RawTransaction],
            },
            "Chase Savings": {...}
        }
    """
    lines = [l.rstrip() for l in text.split("\n") if l.strip()]

    accounts: Dict[str, dict] = {}
    current_account_label = None
    in_transaction_block = False

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        header_match = ACCOUNT_HEADER_RE.search(line)
        if header_match:
            current_account_label = "Chase Total Checking" if "CHECKING" in header_match.group(1).upper() \
                else "Chase Savings"
            accounts.setdefault(current_account_label, {
                "account_number": None,
                "beginning_balance": None,
                "ending_balance": None,
                "transactions": [],
            })
            i += 1
            continue

        acct_num_match = ACCOUNT_NUMBER_RE.search(line)
        if acct_num_match and current_account_label:
            accounts[current_account_label]["account_number"] = acct_num_match.group(1)
            i += 1
            continue

        begin_match = BEGIN_BAL_RE.search(line)
        if begin_match and current_account_label:
            if accounts[current_account_label]["beginning_balance"] is None:
                accounts[current_account_label]["beginning_balance"] = parse_money(begin_match.group(1))
            i += 1
            continue

        end_match = END_BAL_RE.search(line)
        if end_match and current_account_label:
            accounts[current_account_label]["ending_balance"] = parse_money(end_match.group(1))
            i += 1
            continue

        if "*start*transactiondetail" in line.replace(" ", "").lower():
            in_transaction_block = True
            i += 1
            continue
        if "*end*transactiondetail" in line.replace(" ", "").lower():
            in_transaction_block = False
            i += 1
            continue

        date_match = DATE_RE.match(line)
        if date_match and current_account_label:
            mm, dd = date_match.groups()
            rest = line[date_match.end():].strip()

            dup_date_match = DATE_RE.match(rest)
            if dup_date_match:
                rest = rest[dup_date_match.end():].strip()

            money_matches = MONEY_RE.findall(rest)
            if len(money_matches) >= 2:
                amount = parse_money(money_matches[-2])
                balance_after = parse_money(money_matches[-1])
                description = MONEY_RE.sub("", rest).strip()
                description = re.sub(r"\s{2,}", " ", description)
                txn = RawTransaction(
                    date=to_iso_date(mm, dd, statement_year, statement_start_month),
                    description=description,
                    transaction_currency="USD",
                    transaction_amount=amount,
                    settlement_currency="USD",
                    settlement_amount=amount,
                    balance_after=balance_after,
                    raw_source_lines=[line],
                )
                accounts[current_account_label]["transactions"].append(txn)
            i += 1
            continue

        i += 1

    # ACCOUNT_HEADER_RE is a broad substring match, so marketing boilerplate
    # ("...personal Chase savings accounts (excluding Chase Premier...")
    # can trigger a phantom label switch before the genuine section ever
    # appears — harmless when the real section follows later and fills in
    # the same dict entry, but for a single-account statement (e.g. someone
    # who only has Checking, no linked Savings) the phantom "Chase Savings"
    # entry never gets real balances or transactions. Drop any entry that
    # never accumulated either, rather than passing an empty, balance-less
    # account through to reconcile() where it would crash comparing None.
    return {
        label: data for label, data in accounts.items()
        if data["transactions"] or data["beginning_balance"] is not None or data["ending_balance"] is not None
    }


def reconcile(transactions: List[RawTransaction], beginning_balance: float,
              ending_balance: float, **_context) -> ReconciliationResult:
    """
    Validate beginning_balance + sum(settlement_amount) == ending_balance,
    AND that each transaction's balance_after matches a running total
    (catches a single mis-split line, not just an aggregate that happens
    to cancel out).
    """
    bal = beginning_balance
    mismatches = []
    for t in transactions:
        bal += t.settlement_amount
        if t.balance_after is not None and abs(bal - t.balance_after) > 0.01:
            mismatches.append({
                "date": t.date,
                "description": t.description,
                "computed_balance": round(bal, 2),
                "stated_balance": t.balance_after,
                "diff": round(bal - t.balance_after, 2),
            })
    final_diff = round(bal - ending_balance, 2)
    return ReconciliationResult(
        computed_balance=round(bal, 2),
        stated_balance=ending_balance,
        diff=final_diff,
        mismatches=mismatches,
    )
