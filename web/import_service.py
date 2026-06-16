"""
PDF statement detection and async import job management.

Detection: reads first 3 pages of text, pattern-matches to identify
  which account/parser to use, and extracts the statement year + start month.

Job management: a lightweight in-memory job store backed by a threading.Lock.
  Jobs survive as long as the server process runs; a restart clears history.
  Temp PDF files are stored in TEMP_DIR and deleted by the worker thread
  when the job completes (success or failure).
"""
import os
import re
import uuid
import threading
import tempfile
from contextlib import closing
from datetime import datetime
from typing import Optional

TEMP_DIR = os.path.join(tempfile.gettempdir(), "ledger_imports")
os.makedirs(TEMP_DIR, exist_ok=True)

_jobs: dict = {}       # job_id -> dict
_jobs_lock = threading.Lock()

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ── Detection ─────────────────────────────────────────────────────────────────

def detect_pdf(pdf_path: str) -> dict:
    """
    Identify the statement type and period from the PDF.

    Returns a dict with:
      account_type:  "hsbc" | "chase_bank" | "sapphire" | None
      account_label: human-readable name
      year:          int | None
      start_month:   int | None
      period_label:  "Jan 2026" | None
      confidence:    "high" | "medium" | "low"
      notes:         explanation string
    """
    try:
        text = _extract_text_brief(pdf_path, pages=3)
    except Exception as exc:
        return _unknown(f"Could not read PDF: {exc}")

    return (
        _try_hsbc(text)
        or _try_sapphire(text)
        or _try_chase_bank(text)
        or _unknown("Statement format not recognised — please select account type manually.")
    )


def _extract_text_brief(pdf_path: str, pages: int = 3) -> str:
    import pdfplumber
    parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:pages]:
            t = page.extract_text()
            if t:
                parts.append(t)
    return "\n".join(parts)


def _extract_last4(text: str) -> Optional[str]:
    """
    Last 4 digits of the account/card number, used to auto-match a specific
    account (not just a statement format) so a batch of statements for the
    same account doesn't need re-picking per file. Handles both masked card
    numbers ("XXXX XXXX XXXX 1234") and unmasked bank account numbers
    (a long digit string — we just take its last 4 digits).
    """
    m = re.search(r"(?:X{4}\s*){3}(\d{4})\b", text)
    if m:
        return m.group(1)
    m = re.search(r"Account Number:?\s*(\d{6,})", text)
    if m:
        return m.group(1)[-4:]
    return None


def _try_hsbc(text: str) -> Optional[dict]:
    if not re.search(r"\bHSBC\b", text, re.IGNORECASE):
        return None
    # Require at least one HSBC-specific structural marker
    if not re.search(r"BALANCE.*FORWARD|Payment type and details|SORT CODE", text, re.IGNORECASE):
        return None

    # Dates appear as "1 Jan 25" or "31 Dec 24"
    date_match = re.search(
        r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{2})\b",
        text, re.IGNORECASE
    )
    year, start_month, period_label = None, None, None
    if date_match:
        yy = int(date_match.group(3))
        year = 2000 + yy
        start_month = MONTH_NAMES.get(date_match.group(2).lower())
        period_label = f"{date_match.group(2).capitalize()} {year}"

    # HSBC prints the sort code + account number together, e.g.
    # "12-34-56 87654321" — no masking, so take the account number's own
    # last 4 digits directly rather than via _extract_last4()'s generic
    # "Account Number:" label match (HSBC doesn't use that exact label).
    last4 = None
    acct_num_match = re.search(r"\b(\d{2}-\d{2}-\d{2})\s+(\d{6,})\b", text)
    if acct_num_match:
        last4 = acct_num_match.group(2)[-4:]

    return {
        "account_type": "hsbc",
        "account_label": "HSBC UK",
        "year": year, "start_month": start_month, "period_label": period_label,
        "confidence": "high" if date_match else "medium",
        "notes": "Detected HSBC UK statement." + (f" Period: {period_label}." if period_label else ""),
        "last4": last4,
    }


def _try_sapphire(text: str) -> Optional[dict]:
    # Brand name — normal text OR the doubled-char artefact Chase PDFs sometimes
    # produce. NOT reliable on its own: some months' statements only mention
    # "Sapphire Reserve" in a promotional blurb that isn't always present
    # (confirmed missing on real statements once the promo expired), so the
    # card name can't be the sole signal.
    has_brand = (
        re.search(r"Sapphire Reserve", text, re.IGNORECASE)
        or re.search(r"S+a+p+h+i+r+e+\s+R+e+s+e+r+v+e+", text, re.IGNORECASE)
    )
    # Structural fallback: every Chase credit-card billing statement prints
    # "Opening/Closing Date MM/DD/YY - MM/DD/YY" in its Account Summary box.
    # Chase's own checking/savings statements use different wording ("May 22,
    # 2025 through June 23, 2025"), so this phrase + a Chase mention reliably
    # distinguishes the card statement even when the brand name is absent.
    is_chase_card_statement = (
        re.search(r"Opening/Closing Date", text, re.IGNORECASE)
        and re.search(r"\bChase\b", text, re.IGNORECASE)
    )
    if not (has_brand or is_chase_card_statement):
        return None

    year, start_month = _extract_chase_period(text)
    period_label = _period_label(start_month, year)
    return {
        "account_type": "sapphire",
        "account_label": "Chase Sapphire Reserve",
        "year": year, "start_month": start_month, "period_label": period_label,
        "confidence": "high" if (has_brand or year) else "medium",
        "notes": "Detected Chase Sapphire Reserve." + (f" Period: {period_label}." if period_label else ""),
        "last4": _extract_last4(text),
    }


def _try_chase_bank(text: str) -> Optional[dict]:
    if not re.search(r"CHASE TOTAL CHECKING|CHASE SAVINGS|JPMorgan Chase Bank", text, re.IGNORECASE):
        return None

    year, start_month = _extract_chase_period(text)
    period_label = _period_label(start_month, year)
    return {
        "account_type": "chase_bank",
        "account_label": "Chase Checking / Savings",
        "year": year, "start_month": start_month, "period_label": period_label,
        "confidence": "high" if year else "medium",
        "notes": "Detected Chase Checking / Savings statement." + (f" Period: {period_label}." if period_label else ""),
        # Only meaningful for a single-account statement (e.g. someone's
        # standalone Checking-only PDF). A combined Checking+Savings PDF has
        # two account numbers; this picks up whichever appears first in the
        # text, which isn't reliable enough to auto-select a target account
        # for that case — the picker just falls back to manual choice then.
        "last4": _extract_last4(text),
    }


def _extract_chase_period(text: str):
    """Return (year, start_month) from a Chase-format statement."""
    # "April 22, 2026" style
    m = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+\d{1,2},\s+(\d{4})",
        text, re.IGNORECASE,
    )
    if m:
        return int(m.group(2)), MONTH_NAMES.get(m.group(1).lower())

    # "Opening/Closing Date MM/DD/YY - MM/DD/YY" (Chase Sapphire Reserve)
    m = re.search(r"Opening/Closing Date\s+(\d{2})/(\d{2})/(\d{2})", text, re.IGNORECASE)
    if m:
        return 2000 + int(m.group(3)), int(m.group(1))

    # "Opening Date MM/DD/YY"
    m = re.search(r"Opening Date\s+(\d{2})/(\d{2})/(\d{2})", text, re.IGNORECASE)
    if m:
        return 2000 + int(m.group(3)), int(m.group(1))

    # "Statement Period: MM/DD/YYYY – MM/DD/YYYY"
    m = re.search(r"Statement Period.*?(\d{2})/(\d{2})/(\d{4})", text, re.IGNORECASE)
    if m:
        return int(m.group(3)), int(m.group(1))

    return None, None


def _period_label(month: Optional[int], year: Optional[int]) -> Optional[str]:
    if not month or not year:
        return None
    return ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][month - 1] + f" {year}"


def _unknown(notes: str) -> dict:
    return {
        "account_type": None, "account_label": None,
        "year": None, "start_month": None, "period_label": None,
        "confidence": "low", "notes": notes,
    }


# ── Job store ─────────────────────────────────────────────────────────────────

def start_job(temp_id: str, filename: str, account_type: str,
              year: Optional[int], start_month: Optional[int],
              target_account_id: Optional[int] = None) -> str:
    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id":            job_id,
            "temp_id":           temp_id,
            "filename":          filename,
            "account_type":      account_type,
            "year":              year,
            "start_month":       start_month,
            "target_account_id": target_account_id,
            "status":            "queued",
            "inserted":          0,
            "error":             None,
            "started_at":        datetime.now().isoformat(timespec="seconds"),
            "finished_at":       None,
        }
    t = threading.Thread(target=_run_job, args=(job_id,), daemon=True)
    t.start()
    return job_id


def get_jobs() -> dict:
    with _jobs_lock:
        return dict(_jobs)


def _run_job(job_id: str) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
        _jobs[job_id]["status"] = "running"

    pdf_path = os.path.join(TEMP_DIR, job["temp_id"] + ".pdf")
    try:
        # Import inline to avoid circular import at module load time
        import sys, os as _os
        _os.sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))
        from persistence.database import get_connection
        from cli.ingest import ingest_hsbc, ingest_chase_bank, ingest_sapphire

        with closing(get_connection()) as conn:
            acct = job["account_type"]
            year = job["year"] or datetime.now().year
            sm   = job["start_month"] or 1
            target_account_id = job.get("target_account_id")

            if acct == "hsbc":
                inserted_ids, skipped, reconciled, diff = ingest_hsbc(conn, pdf_path, target_account_id=target_account_id)
            elif acct == "chase_bank":
                inserted_ids, skipped, reconciled, diff = ingest_chase_bank(conn, pdf_path, year, sm, target_account_id=target_account_id)
            elif acct == "sapphire":
                inserted_ids, skipped, reconciled, diff = ingest_sapphire(conn, pdf_path, year, sm, target_account_id=target_account_id)
            else:
                raise ValueError(f"Unknown account type: {acct}")
            conn.commit()

        with _jobs_lock:
            _jobs[job_id].update({
                "status":      "done",
                "inserted":    len(inserted_ids),
                "skipped":     skipped,
                "reconciled":  reconciled,
                "diff":        diff,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            })

    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id].update({
                "status":      "error",
                "error":       str(exc),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            })
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)
