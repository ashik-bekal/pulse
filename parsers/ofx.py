"""
OFX / QFX statement parser.

OFX (Open Financial Exchange) is the machine-readable format exported by most
US and UK banks. It exists in two flavours:
  OFX 1.x — a legacy SGML dialect where leaf elements have no closing tags
             (<CURDEF>USD with no </CURDEF>). Python's XML parser rejects it.
  OFX 2.x — proper XML, parseable directly.

Both are handled transparently by the ofxparse library. This module only deals
with the canonical RawTransaction shape; it doesn't know about PDFs or SQLite.

Key differences from PDF parsers:
- No per-transaction running balance → balance_after is always None.
- No FX breakout: standard OFX carries one amount in the account's own
  currency. Both transaction_currency and settlement_currency are set to the
  account currency (CURDEF). Foreign-currency transactions show only the
  settled amount, not the native amount + rate.
- Reconciliation is partial: OFX provides a LEDGERBAL (point-in-time closing
  balance) but not the opening balance for the period. The reconcile() function
  records the closing balance as the snapshot but cannot verify the full
  opening → sum → closing walk, so is_clean is always False when LEDGERBAL is
  present (marked "unverified" in the dashboard), or True when no LEDGERBAL
  exists (nothing to check).
"""
import io
from typing import List, Optional

from domain.models import RawTransaction, ReconciliationResult


def _ofx(text: str):
    """Parse OFX content (string) and return an ofxparse Ofx object."""
    from ofxparse import OfxParser
    return OfxParser.parse(io.BytesIO(text.encode("latin-1")))


def parse(text: str, **context) -> List[RawTransaction]:
    """
    Parse an OFX file's text content into canonical RawTransactions.

    Each <STMTTRN> becomes one RawTransaction. Description is the NAME tag,
    supplemented by MEMO when both are present and differ. FITID is stored in
    raw_source_lines for audit traceability.
    """
    ofx = _ofx(text)
    acct = ofx.account
    stmt = acct.statement
    currency = (
        getattr(stmt, "currency", None)
        or getattr(acct, "currency", None)
        or "USD"
    ).upper()

    result = []
    for t in stmt.transactions:
        payee = (getattr(t, "payee", "") or "").strip()
        memo  = (getattr(t, "memo",  "") or "").strip()
        if payee and memo and memo != payee:
            description = f"{payee} {memo}"
        else:
            description = payee or memo or ""

        amount = float(t.amount)
        date_str = t.date.strftime("%Y-%m-%d")
        fitid = getattr(t, "id", None) or ""

        result.append(RawTransaction(
            date=date_str,
            description=description,
            transaction_currency=currency,
            transaction_amount=amount,
            settlement_currency=currency,
            settlement_amount=amount,
            fx_rate=None,
            balance_after=None,
            raw_source_lines=[fitid] if fitid else [],
        ))
    return result


def reconcile(
    transactions: List[RawTransaction],
    closing_balance: Optional[float] = None,
    **_context,
) -> ReconciliationResult:
    """
    OFX reconciliation is partial: we have a closing balance (LEDGERBAL) but
    not the opening balance. We record the stated closing balance as the
    snapshot and mark reconciliation as unverified (is_clean=False) so the
    dashboard surfaces it for review rather than silently accepting it.

    When closing_balance is None (file has no LEDGERBAL), we return a clean
    result with no stated balance — nothing to check, nothing to record.
    """
    if closing_balance is None:
        return ReconciliationResult(
            computed_balance=None,
            stated_balance=None,
            diff=None,
        )

    return ReconciliationResult(
        computed_balance=None,
        stated_balance=closing_balance,
        diff=None,
        mismatches=[{
            "note": (
                "OFX format — opening balance not available; "
                "closing balance taken from LEDGERBAL. "
                "Full period reconciliation cannot be verified."
            )
        }],
    )


def extract_ledger_balance(text: str) -> Optional[float]:
    """Return the LEDGERBAL amount from the OFX file, or None if absent."""
    try:
        bal = _ofx(text).account.statement.balance
        return float(bal) if bal is not None else None
    except Exception:
        return None


def extract_statement_end_date(text: str) -> Optional[str]:
    """
    Return the best available statement-end date as 'YYYY-MM' for snapshot
    month keying.  Priority: DTASOF (LEDGERBAL date) > DTEND > None.
    """
    try:
        stmt = _ofx(text).account.statement
        for attr in ("balance_date", "end_date"):
            dt = getattr(stmt, attr, None)
            if dt is not None:
                return dt.strftime("%Y-%m")
    except Exception:
        pass
    return None


def extract_account_info(text: str) -> dict:
    """
    Extract account metadata from an OFX file for auto-detection.

    Returns a dict with:
      last4       — last 4 digits of ACCTID (str or None)
      account_type — "CHECKING" | "SAVINGS" | "CREDITLINE" | "MONEYMRKT" | None
      currency    — CURDEF string (e.g. "USD", "GBP")
      start_date  — DTSTART as datetime, or None
      end_date    — DTEND as datetime, or None
    """
    try:
        ofx = _ofx(text)
        acct = ofx.account
        stmt = acct.statement
        acct_id = getattr(acct, "account_id", None) or ""
        raw_ccy = getattr(stmt, "currency", None) or getattr(acct, "currency", None) or "USD"
        return {
            "last4":        acct_id[-4:] if len(acct_id) >= 4 else acct_id or None,
            "account_type": getattr(acct, "account_type", None),
            "currency":     raw_ccy.upper(),
            "start_date":   getattr(stmt, "start_date", None),
            "end_date":     getattr(stmt, "end_date",   None),
        }
    except Exception:
        return {"last4": None, "account_type": None, "currency": "USD",
                "start_date": None, "end_date": None}
