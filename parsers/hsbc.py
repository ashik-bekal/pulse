"""
HSBC UK statement parser.

The block-segmentation algorithm below is UNCHANGED from the version
validated against real statements (zero reconciliation diff). Only the
output shape changed: instead of returning a bespoke ParsedTransaction
with separate paid_out/paid_in fields, this now emits the canonical
RawTransaction directly, with the sign/currency normalization that used
to live in the ingestion script folded in here where it belongs (a parser
should hand back data the rest of the app can use without per-format
branching).

Layout notes (from real statement inspection), preserved verbatim:
- Date appears once per day-group; subsequent transactions on the same day
  do NOT repeat the date.
- Each transaction line is prefixed with a type code: DD, SO, VIS, ))), CR, DR, BP, FP, TFR, ATM, CHQ
- Running balance is only printed on the LAST line of a day-group, not every
  line. Validation is therefore done per balance-checkpoint, not per-line.
- Foreign currency purchases print a 2-line breakout:
      EUR 45.00 @ 1.1544
      Visa Rate 38.98          <- this is what hits the GBP balance
  This account is GBP-denominated, so:
      transaction_currency = EUR (the foreign currency named)
      transaction_amount   = 45.00 (negative for an outflow)
      settlement_currency  = GBP (account's own currency)
      settlement_amount    = 38.98 (negative for an outflow; what hit the balance)
- "DR CHARGE NON-STERLING CASH FEE" and "Non-Sterling Transaction Fee" lines
  are split into their own fee transactions (GBP, no FX breakout).
"""
import re
from typing import List, Optional

from domain.models import RawTransaction, ReconciliationResult
from parsers.base import MONEY_RE, parse_money

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

DATE_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3})\s+(\d{2})\b")
# BP = Banker's Payment, FP = Faster Payment, TFR = Transfer, ATM = cash withdrawal, CHQ = cheque
TYPE_CODE_RE = re.compile(r"^(DD|SO|VIS|\)\)\)|CR|DR|BP|FP|TFR|ATM|CHQ|OBP|BGC|STO|BAC)(?=\s|$)")
FX_BREAKOUT_RE = re.compile(r"([A-Z]{3})\s+([\d,]+\.\d{2})\s*@\s*([\d.]+)")
VISA_RATE_RE = re.compile(r"Visa Rate\s+([\d,]+\.\d{2})")
NONSTERLING_FEE_RE = re.compile(r"Non-Sterling\s*Transaction\s*Fee\s*([\d,]+\.\d{2})?", re.IGNORECASE)
NONSTERLING_CASH_FEE_RE = re.compile(r"NON-STERLING\s*CASH\s*FEE\s*([\d,]+\.\d{2})?", re.IGNORECASE)
BALANCE_FWD_RE = re.compile(r"BALANCE\s*(BROUGHT|CARRIED)\s*FORWARD", re.IGNORECASE)

# Type codes whose sign is unambiguously debit or credit — never flip these.
_FIXED_DEBIT  = {'DD', 'SO', 'ATM', 'CHQ', 'DR', 'OBP', 'BGC', 'STO', 'BAC'}
_FIXED_CREDIT = {'CR'}
ACCOUNT_SUMMARY_OPENING_RE = re.compile(r"Opening\s*Balance\s*[£$]?\s*([\d,]+\.\d{2})", re.IGNORECASE)
TABLE_HEADER_RE = re.compile(r"Date\s+Payment type and details", re.IGNORECASE)
TABLE_END_MARKERS = [
    "Information about the Financial Services",
    "Credit Interest Rates",
    "AER Overdraft EAR",
]


def _correct_signs(records, opening_balance):
    """
    Use balance checkpoints to fix the sign of ambiguous type codes (VIS, FP,
    TFR, BP, …) that can be either credits or debits.

    Algorithm: walk transactions in order, grouping them between consecutive
    balance checkpoints.  For each group, if the signed sum doesn't match
    the expected balance delta, try all 2^n sign combinations for the
    ambiguous transactions in that group and apply the first combination
    that produces a matching delta.

    records: list of mutable dicts with keys:
        code, sign (+1/-1), unsigned_amt, balance_after (or None), is_fx
    """
    if opening_balance is None:
        return

    FIXED = _FIXED_DEBIT | _FIXED_CREDIT
    bal = opening_balance
    group_start = 0

    for i, rec in enumerate(records):
        if rec['balance_after'] is None:
            continue

        group = records[group_start:i + 1]
        expected_delta = rec['balance_after'] - bal
        current_delta  = sum(r['sign'] * r['unsigned_amt'] for r in group)

        if abs(current_delta - expected_delta) > 0.005:
            ambig = [j for j, r in enumerate(group)
                     if not r['is_fx'] and r.get('code') not in FIXED]

            for mask in range(2 ** len(ambig)):
                signs = [r['sign'] for r in group]
                for bit, gi in enumerate(ambig):
                    if (mask >> bit) & 1:
                        signs[gi] *= -1
                trial = sum(s * r['unsigned_amt'] for s, r in zip(signs, group))
                if abs(trial - expected_delta) < 0.005:
                    for bit, gi in enumerate(ambig):
                        if (mask >> bit) & 1:
                            group[gi]['sign'] *= -1
                    break

        bal = rec['balance_after']
        group_start = i + 1


def _to_iso_date(day, mon_abbr, yy):
    return f"{2000+int(yy):04d}-{MONTHS[mon_abbr.lower()]:02d}-{int(day):02d}"


class _Block:
    """Internal accumulator for one transaction's worth of raw text + amounts.
    Not exported — an implementation detail of the block-segmentation algorithm."""
    __slots__ = ("date", "lines", "type_code")

    def __init__(self, date, type_code):
        self.date = date
        self.lines: List[str] = []
        self.type_code = type_code


def _isolate_transaction_lines(raw_lines: list) -> list:
    """
    HSBC statements interleave the transaction table with page headers/footers
    and a trailing disclosures section, repeated per page. Keep only lines
    between "Date Payment type and details..." (or, on continuation pages,
    immediately after "Your Bank Account details") and the next
    BALANCE CARRIED FORWARD / disclosures marker.
    """
    keep = []
    in_table = False
    for line in raw_lines:
        if TABLE_HEADER_RE.search(line) or "Your Bank Account details" in line:
            in_table = True
            continue
        if any(marker in line for marker in TABLE_END_MARKERS):
            in_table = False
            continue
        if BALANCE_FWD_RE.search(line) and "CARRIED" in line.upper():
            if in_table:
                keep.append(line)
            in_table = False
            continue
        if not in_table:
            continue
        stripped = line.strip()
        if stripped == "A":
            continue
        if re.match(r"^\d+ Centenary Square", stripped):
            continue
        keep.append(line)
    return keep


def _parse_with_balances(text: str):
    """
    Internal: parse HSBC statement text into canonical RawTransaction
    objects plus the statement's opening/closing balance. See module-level
    parse() and parse_with_balances() for the public entry points.
    """
    all_lines = [l.rstrip() for l in text.split("\n") if l.strip()]

    # Scan Account Summary section (outside the transaction table) for an
    # opening balance. This catches new accounts whose first statement has
    # no "BALANCE BROUGHT FORWARD" line (opening balance is £0.00 or similar).
    summary_opening = None
    for line in all_lines:
        m = ACCOUNT_SUMMARY_OPENING_RE.search(line)
        if m:
            summary_opening = parse_money(m.group(1))
            break

    raw_lines = _isolate_transaction_lines(all_lines)

    opening_balance = None
    closing_balance = None
    checkpoints = []

    # Pass 1: tag each line with running date context; strip BALANCE FWD lines
    tagged = []
    current_date = None
    for line in raw_lines:
        if BALANCE_FWD_RE.search(line):
            money = MONEY_RE.findall(line)
            if money:
                bal = parse_money(money[-1])
                if "BROUGHT" in line.upper() and opening_balance is None:
                    opening_balance = bal
                closing_balance = bal
                if current_date:
                    checkpoints.append((current_date, bal))
            continue
        date_match = DATE_RE.match(line)
        if date_match:
            current_date = _to_iso_date(*date_match.groups())
            rest = line[date_match.end():].strip()
            if rest:
                tagged.append((current_date, rest))
            continue
        tagged.append((current_date, line))

    # Fall back to Account Summary opening balance when transaction table
    # has no "BALANCE BROUGHT FORWARD" line (e.g. first statement of new account).
    if opening_balance is None and summary_opening is not None:
        opening_balance = summary_opening

    # Pass 2: group into blocks. New block starts at a type-code line.
    blocks: List[_Block] = []
    for date, line in tagged:
        if TYPE_CODE_RE.match(line) or not blocks:
            type_match = TYPE_CODE_RE.match(line)
            code = type_match.group(1) if type_match else None
            blocks.append(_Block(date, code))
            blocks[-1].lines.append(line)
        else:
            blocks[-1].lines.append(line)

    # Pass 3: parse each block into an intermediate mutable dict.
    # We defer creating frozen RawTransaction objects until after sign correction.
    intermediates: List[dict] = []

    for block in blocks:
        full_text = " ".join(block.lines)
        code = block.type_code

        fee_amt = None
        fee_match = NONSTERLING_FEE_RE.search(full_text)
        cash_fee_match = NONSTERLING_CASH_FEE_RE.search(full_text)
        if fee_match and fee_match.group(1):
            fee_amt = parse_money(fee_match.group(1))
        if cash_fee_match and cash_fee_match.group(1):
            fee_amt = parse_money(cash_fee_match.group(1))

        fx_match = FX_BREAKOUT_RE.search(full_text)
        visa_rate_match = VISA_RATE_RE.search(full_text)

        if fx_match and visa_rate_match:
            ccy, native_amt_str, rate_str = fx_match.groups()
            native_amt = parse_money(native_amt_str)
            fx_rate = float(rate_str)
            settle_amt = parse_money(visa_rate_match.group(1))

            desc_lines = []
            for bl in block.lines:
                if FX_BREAKOUT_RE.search(bl) or VISA_RATE_RE.search(bl) or \
                   NONSTERLING_FEE_RE.search(bl) or NONSTERLING_CASH_FEE_RE.search(bl):
                    continue
                desc_lines.append(bl)
            desc = " ".join(desc_lines)
            desc = TYPE_CODE_RE.sub("", desc, count=1).strip()
            description = re.sub(r"\s{2,}", " ", desc).strip()

            all_money = MONEY_RE.findall(full_text)
            known_vals = {round(native_amt, 2), round(settle_amt, 2)}
            if fee_amt is not None:
                known_vals.add(round(fee_amt, 2))
            balance_after = None
            for m in all_money:
                v = round(parse_money(m), 2)
                if v not in known_vals:
                    balance_after = v
                    checkpoints.append((block.date, v))
                    break

            # FX outflow — sign is always negative; skip sign correction.
            intermediates.append({
                'date': block.date, 'code': code, 'description': description,
                'is_fx': True, 'sign': -1,
                'unsigned_amt': settle_amt, 'balance_after': balance_after,
                'fx_ccy': ccy, 'fx_native': native_amt, 'fx_rate': fx_rate,
                'raw_source_lines': list(block.lines),
            })
            if fee_amt is not None:
                intermediates.append({
                    'date': block.date, 'code': code,
                    'description': "Non-Sterling Transaction Fee",
                    'is_fx': True, 'sign': -1,
                    'unsigned_amt': fee_amt, 'balance_after': None,
                    'fx_ccy': None, 'fx_native': None, 'fx_rate': None,
                    'raw_source_lines': list(block.lines),
                })
            continue

        # Non-FX block
        desc = full_text
        if code:
            desc = TYPE_CODE_RE.sub("", desc, count=1).strip()
        money_tokens = MONEY_RE.findall(desc)
        desc = MONEY_RE.sub("", desc).strip()
        description = re.sub(r"\s{2,}", " ", desc).strip()

        if fee_amt is not None:
            balance_after = None
            if len(money_tokens) > 1:
                balance_after = parse_money(money_tokens[-1])
                checkpoints.append((block.date, balance_after))
            intermediates.append({
                'date': block.date, 'code': code,
                'description': "Non-Sterling Transaction Fee",
                'is_fx': False, 'sign': -1,
                'unsigned_amt': fee_amt, 'balance_after': balance_after,
                'fx_ccy': None, 'fx_native': None, 'fx_rate': None,
                'raw_source_lines': list(block.lines),
            })
            continue

        if not money_tokens:
            continue

        amt = parse_money(money_tokens[0])
        balance_after = None
        if len(money_tokens) > 1:
            balance_after = parse_money(money_tokens[-1])
            checkpoints.append((block.date, balance_after))

        initial_sign = +1 if code in _FIXED_CREDIT else -1
        intermediates.append({
            'date': block.date, 'code': code, 'description': description,
            'is_fx': False, 'sign': initial_sign,
            'unsigned_amt': amt, 'balance_after': balance_after,
            'fx_ccy': None, 'fx_native': None, 'fx_rate': None,
            'raw_source_lines': list(block.lines),
        })

    # Pass 4: correct signs of ambiguous transactions using balance checkpoints.
    _correct_signs(intermediates, opening_balance)

    # Pass 5: emit frozen RawTransaction objects.
    transactions: List[RawTransaction] = []
    for rec in intermediates:
        signed_amt = rec['sign'] * rec['unsigned_amt']
        if rec['fx_ccy']:
            transactions.append(RawTransaction(
                date=rec['date'],
                description=rec['description'],
                transaction_currency=rec['fx_ccy'],
                transaction_amount=rec['sign'] * rec['fx_native'],
                settlement_currency="GBP",
                settlement_amount=signed_amt,
                fx_rate=rec['fx_rate'],
                balance_after=rec['balance_after'],
                raw_source_lines=rec['raw_source_lines'],
            ))
        else:
            transactions.append(RawTransaction(
                date=rec['date'],
                description=rec['description'],
                transaction_currency="GBP",
                transaction_amount=signed_amt,
                settlement_currency="GBP",
                settlement_amount=signed_amt,
                balance_after=rec['balance_after'],
                raw_source_lines=rec['raw_source_lines'],
            ))

    return transactions, opening_balance, closing_balance


def parse(text: str, **context) -> List[RawTransaction]:
    """Public parse() conforms to the StatementParser protocol: text in,
    transactions out. Use parse_with_balances() if you also need the
    statement's opening/closing balance for reconciliation."""
    transactions, _, _ = _parse_with_balances(text)
    return transactions


def parse_with_balances(text: str):
    """Returns (transactions, opening_balance, closing_balance). Preferred
    entry point when you'll also call reconcile() afterward, since it avoids
    re-parsing the text twice to recover the balances."""
    return _parse_with_balances(text)


def reconcile(transactions: List[RawTransaction], opening_balance: Optional[float],
              closing_balance: Optional[float] = None, **_context) -> ReconciliationResult:
    """
    Walk opening_balance through each transaction's settlement_amount and
    confirm it reproduces every printed balance_after checkpoint, ending at
    closing_balance.

    If opening_balance is None (not found in the PDF — e.g. continuation
    statement or unexpected layout), reconciliation is skipped and the result
    reflects that with diff=None and a single mismatch note.
    """
    if opening_balance is None:
        return ReconciliationResult(
            computed_balance=None,
            stated_balance=closing_balance,
            diff=None,
            mismatches=[{"note": "Opening balance not found — reconciliation skipped"}],
        )
    bal = opening_balance
    mismatches = []
    for t in transactions:
        bal += (t.settlement_amount or 0.0)
        if t.balance_after is not None:
            if abs(bal - t.balance_after) > 0.01:
                mismatches.append({
                    "date": t.date,
                    "description": t.description,
                    "computed_balance": round(bal, 2),
                    "stated_balance": t.balance_after,
                    "diff": round(bal - t.balance_after, 2),
                })
    final_diff = round(bal - closing_balance, 2) if closing_balance is not None else None
    return ReconciliationResult(
        computed_balance=round(bal, 2),
        stated_balance=closing_balance,
        diff=final_diff,
        mismatches=mismatches,
    )
