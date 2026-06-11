"""
Chase Sapphire Reserve statement parser.

Algorithm unchanged from the validated version (zero reconciliation diff,
all FX sanity checks passing against real statements). Output shape changed
to canonical RawTransaction.

Layout notes preserved verbatim:
- Account is USD-denominated; billed amount always USD (settlement side).
- Domestic transaction: single line "MM/DD MERCHANT 12.34"
- Foreign transaction: THREE lines
      MM/DD MERCHANT NAME CITY        16.31      <- txn date + USD billed amount
      MM/DD CURRENCY NAME                         <- POSTING date (not txn date!) + currency name
      12.05 X 1.353526970 (EXCHG RATE)            <- native amount X rate
  The transaction_date for the whole entry comes from LINE 1 only.
- Currency is spelled out in English ("POUND STERLING"), not an ISO code -
  CURRENCY_NAME_MAP translates it.
- Confirmed: no foreign-transaction fee on this card; none of the lines
  represent a fee, so none is synthesized.
- Some Chase PDF headers render with every character doubled due to a
  bold/shadow text effect (e.g. "AACCCCOOUUNNTT AACCTTIIVVIITTYY" for
  "ACCOUNT ACTIVITY") - _normalize_doubled_chars() undoes this for section
  detection only; it does not touch transaction text.
"""
import re
from typing import List, Optional

from domain.models import RawTransaction, ReconciliationResult
from parsers.base import parse_money, to_iso_date

CURRENCY_NAME_MAP = {
    "POUND STERLING": "GBP",
    "EURO": "EUR",
    "INDIAN RUPEE": "INR",
    "US DOLLAR": "USD",
    "CANADIAN DOLLAR": "CAD",
    "AUSTRALIAN DOLLAR": "AUD",
    "SWISS FRANC": "CHF",
    "JAPANESE YEN": "JPY",
    "MEXICAN PESO": "MXN",
    "THAI BAHT": "THB",
    "SINGAPORE DOLLAR": "SGD",
    "HONG KONG DOLLAR": "HKD",
    "NEW ZEALAND DOLLAR": "NZD",
    "SOUTH AFRICAN RAND": "ZAR",
    "UAE DIRHAM": "AED",
    "TURKISH LIRA": "TRY",
}

# Amount group allows zero digits before the decimal point ("[\d,]*" not
# "+"): Chase sometimes prints sub-$1 amounts without the leading zero, e.g.
# ".27" instead of "0.27" — with a "+" here that line fails to match at all
# and the transaction is silently dropped.
DATE_LINE_RE = re.compile(r"^(\d{2})/(\d{2})\s+(.*?)\s+(-?[\d,]*\.\d{2})\s*$")
CURRENCY_NAME_LINE_RE = re.compile(
    r"^(\d{2})/(\d{2})\s+(" + "|".join(re.escape(k) for k in CURRENCY_NAME_MAP) + r")\s*$",
    re.IGNORECASE,
)
NATIVE_RATE_RE = re.compile(r"^([\d,]+\.\d{2,})\s*X\s*([\d.]+)\s*\(EXCHG RATE\)", re.IGNORECASE)
SECTION_START_RE = re.compile(r"^(PURCHASE|CASH ADVANCE|FEES CHARGED|INTEREST CHARGED)$", re.IGNORECASE)
# Match "YYYY Totals" for any year, not just the current one — avoids
# the "2026 Totals" literal breaking on January 2027 statements.
STOP_SECTION_RE = re.compile(r"^(\d{4} Totals|INTEREST CHARGES|Year-to-date totals)", re.IGNORECASE)


def _normalize_doubled_chars(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        if i + 1 < len(s) and s[i] == s[i + 1]:
            out.append(s[i])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def parse(text: str, statement_year: int, statement_start_month: int,
          **_context) -> List[RawTransaction]:
    """
    Parse a Chase Sapphire Reserve statement.

    statement_year / statement_start_month: year/month of the statement's
    "Opening/Closing Date" START (e.g. "04/11/26" -> 2026, 4).
    """
    lines = [l.rstrip() for l in text.split("\n") if l.strip()]
    n = len(lines)

    transactions: List[RawTransaction] = []
    i = 0
    in_activity_section = False

    while i < n:
        line = lines[i]

        if "ACCOUNT ACTIVITY" in _normalize_doubled_chars(line.upper()):
            in_activity_section = True
            i += 1
            continue
        if STOP_SECTION_RE.match(line):
            in_activity_section = False
            i += 1
            continue
        if not in_activity_section:
            i += 1
            continue
        if SECTION_START_RE.match(line):
            i += 1
            continue

        m1 = DATE_LINE_RE.match(line)
        if m1:
            mm, dd, desc, amt_str = m1.groups()
            usd_amount = parse_money(amt_str)
            txn_date = to_iso_date(mm, dd, statement_year, statement_start_month)
            description = desc.strip()

            is_foreign = False
            native_currency = None
            native_amount = None
            fx_rate = None

            if i + 1 < n:
                m2 = CURRENCY_NAME_LINE_RE.match(lines[i + 1])
                if m2:
                    _, _, currency_name = m2.groups()
                    native_currency = CURRENCY_NAME_MAP.get(currency_name.upper(), currency_name.upper())
                    is_foreign = True
                    source_lines = [line, lines[i + 1]]

                    if i + 2 < n:
                        m3 = NATIVE_RATE_RE.match(lines[i + 2])
                        if m3:
                            native_amt_str, rate_str = m3.groups()
                            native_amount = parse_money(native_amt_str)
                            fx_rate = float(rate_str)
                            source_lines.append(lines[i + 2])
                            i += 3
                        else:
                            i += 2
                    else:
                        i += 2

                    txn_currency = native_currency
                    txn_amount = -native_amount if native_amount is not None else -usd_amount
                    transactions.append(RawTransaction(
                        date=txn_date,
                        description=description,
                        transaction_currency=txn_currency,
                        transaction_amount=txn_amount,
                        settlement_currency="USD",
                        settlement_amount=-usd_amount,
                        fx_rate=fx_rate,
                        raw_source_lines=source_lines,
                    ))
                    continue

            # Domestic, single-line transaction
            transactions.append(RawTransaction(
                date=txn_date,
                description=description,
                transaction_currency="USD",
                transaction_amount=-usd_amount,
                settlement_currency="USD",
                settlement_amount=-usd_amount,
                raw_source_lines=[line],
            ))
            i += 1
            continue

        i += 1

    return transactions


def extract_balances(text: str):
    """
    Returns (previous_balance, new_balance) from the Account Summary box.

    Both fields can be negative (the account is in credit — payments/refunds
    exceeded purchases that cycle) and Chase prints the sign BEFORE the
    dollar sign, e.g. "New Balance -$3.66", not "New Balance $-3.66".
    """
    prev_match = re.search(r"Previous Balance\s+(-?\$?[\d,]+\.\d{2})", text)
    new_match = re.search(r"New Balance\s+(-?\$?[\d,]+\.\d{2})", text)
    previous_balance = parse_money(prev_match.group(1)) if prev_match else None
    new_balance = parse_money(new_match.group(1)) if new_match else None
    return previous_balance, new_balance


def fx_sanity_failures(transactions: List[RawTransaction], tolerance: float = 0.02) -> List[dict]:
    """
    For every FX transaction, native_amount * fx_rate should reproduce the
    USD billed amount (within rounding). Returns a list of dicts describing
    any transaction where that check fails - these should be surfaced for
    review, not silently trusted.
    """
    failures = []
    for t in transactions:
        if t.fx_rate is None:
            continue
        expected_usd = abs(t.transaction_amount) * t.fx_rate
        actual_usd = abs(t.settlement_amount)
        diff = round(expected_usd - actual_usd, 2)
        if abs(diff) > tolerance:
            failures.append({
                "date": t.date, "description": t.description,
                "native_amount": t.transaction_amount, "native_currency": t.transaction_currency,
                "fx_rate": t.fx_rate, "usd_amount": t.settlement_amount, "diff": diff,
            })
    return failures


def reconcile(transactions: List[RawTransaction], previous_balance: float,
              new_balance: float, payments_credits: float = 0.0,
              fees_charged: float = 0.0, interest_charged: float = 0.0,
              **_context) -> ReconciliationResult:
    """
    previous_balance - payments_credits + purchases + fees + interest == new_balance
    Purchases are derived by summing settlement_amount (all transactions in
    this parser are purchases; payments/fees/interest are passed by the
    caller from the statement's own Account Summary box when present).
    """
    purchases_total = -sum(t.settlement_amount for t in transactions)  # settlement_amount is negative for outflow
    computed = previous_balance - payments_credits + purchases_total + fees_charged + interest_charged
    diff = round(computed - new_balance, 2)
    return ReconciliationResult(
        computed_balance=round(computed, 2),
        stated_balance=new_balance,
        diff=diff,
        mismatches=[],  # Sapphire has no per-line running balance to check against
    )
