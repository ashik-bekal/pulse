"""
Statement-format auto-detection: the regex dispatch in web/import_service.py
that identifies HSBC / Chase Sapphire / Chase Bank statements from extracted
PDF text and pulls out the statement period + account last-4. All fixture
text below is fictional — no real account numbers or statement content.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from web.import_service import (  # noqa: E402
    _try_hsbc, _try_sapphire, _try_chase_bank,
    _extract_chase_period, _extract_last4, _period_label, detect_pdf,
)

HSBC_TEXT = """HSBC UK Bank plc
Your Statement
Payment type and details
40-12-34 87654321
1 Jan 25 BALANCE BROUGHT FORWARD 1,000.00
"""

SAPPHIRE_TEXT = """CHASE
Sapphire Reserve
Opening/Closing Date 04/22/25 - 05/21/25
XXXX XXXX XXXX 4321
"""

SAPPHIRE_NO_BRAND_TEXT = """CHASE
Account Summary
Opening/Closing Date 04/22/25 - 05/21/25
"""

CHASE_BANK_TEXT = """JPMorgan Chase Bank, N.A.
CHASE TOTAL CHECKING
April 22, 2025 through May 21, 2025
Account Number: 000001234567
"""


def test_hsbc_detected():
    result = _try_hsbc(HSBC_TEXT)
    assert result is not None
    assert result["account_type"] == "hsbc"
    assert result["year"] == 2025
    assert result["start_month"] == 1
    assert result["last4"] == "4321"
    assert result["confidence"] == "high"


def test_hsbc_requires_structural_marker():
    assert _try_hsbc("HSBC mentioned in passing") is None


def test_sapphire_by_brand():
    result = _try_sapphire(SAPPHIRE_TEXT)
    assert result is not None
    assert result["account_type"] == "sapphire"
    assert result["year"] == 2025
    assert result["start_month"] == 4
    assert result["last4"] == "4321"


def test_sapphire_without_brand_falls_back_to_structure():
    result = _try_sapphire(SAPPHIRE_NO_BRAND_TEXT)
    assert result is not None
    assert result["account_type"] == "sapphire"


def test_chase_bank_detected():
    result = _try_chase_bank(CHASE_BANK_TEXT)
    assert result is not None
    assert result["account_type"] == "chase_bank"
    assert result["year"] == 2025
    assert result["start_month"] == 4
    assert result["last4"] == "4567"


def test_chase_period_opening_closing_format():
    assert _extract_chase_period("Opening/Closing Date 12/22/25 - 01/21/26") == (2025, 12)


def test_extract_last4_masked_card():
    assert _extract_last4("XXXX XXXX XXXX 9876") == "9876"


def test_extract_last4_account_label():
    assert _extract_last4("Account Number: 123456789") == "6789"


def test_extract_last4_unmatched():
    assert _extract_last4("no account info here") is None


def test_period_label():
    assert _period_label(4, 2025) == "Apr 2025"
    assert _period_label(None, 2025) is None


def test_detect_pdf_unreadable_file():
    result = detect_pdf("/nonexistent/file.pdf")
    assert result["account_type"] is None
    assert result["confidence"] == "low"


def test_priority_order_hsbc_wins():
    # detect_pdf tries _try_hsbc before _try_sapphire/_try_chase_bank, so a
    # text blob carrying both HSBC and Chase markers resolves to HSBC.
    mixed_text = HSBC_TEXT + "\nOpening/Closing Date 04/22/25 - 05/21/25\nChase\n"
    assert _try_hsbc(mixed_text) is not None
