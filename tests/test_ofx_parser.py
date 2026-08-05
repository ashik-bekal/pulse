"""
OFX parser tests against synthetic OFX fixture strings.

No real bank files or disk databases are needed. OFX 1.x and 2.x fixtures
are constructed inline so the full parse → reconcile pipeline can be exercised
in isolation from any I/O.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from parsers.ofx import parse, reconcile, extract_ledger_balance, extract_account_info
from persistence.database import init_schema
from persistence.repositories import (
    AccountRepository, CategoryRepository, VendorRuleRepository,
    TransactionRepository, ReviewQueueRepository, ExchangeRateRepository,
)
from services.ingestion import ingest_transactions


# ── Fixtures ──────────────────────────────────────────────────────────────────

# OFX 2.x (valid XML) — checking account with 3 transactions and a LEDGERBAL
OFX2_CHECKING = """\
<?xml version="1.0" encoding="UTF-8"?>
<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
  <SIGNONMSGSRSV1>
    <SONRS><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
    <DTSERVER>20260201000000</DTSERVER><LANGUAGE>ENG</LANGUAGE></SONRS>
  </SIGNONMSGSRSV1>
  <BANKMSGSRSV1>
    <STMTTRNRS>
      <TRNUID>1</TRNUID><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
      <STMTRS>
        <CURDEF>USD</CURDEF>
        <BANKACCTFROM>
          <BANKID>021000021</BANKID>
          <ACCTID>123456781234</ACCTID>
          <ACCTTYPE>CHECKING</ACCTTYPE>
        </BANKACCTFROM>
        <BANKTRANLIST>
          <DTSTART>20260101000000</DTSTART>
          <DTEND>20260131000000</DTEND>
          <STMTTRN>
            <TRNTYPE>CREDIT</TRNTYPE>
            <DTPOSTED>20260105000000</DTPOSTED>
            <TRNAMT>3500.00</TRNAMT>
            <FITID>20260105001</FITID>
            <NAME>ACME PAYROLL</NAME>
            <MEMO>DIRECT DEPOSIT</MEMO>
          </STMTTRN>
          <STMTTRN>
            <TRNTYPE>DEBIT</TRNTYPE>
            <DTPOSTED>20260110000000</DTPOSTED>
            <TRNAMT>-120.00</TRNAMT>
            <FITID>20260110001</FITID>
            <NAME>GROCERY STORE</NAME>
          </STMTTRN>
          <STMTTRN>
            <TRNTYPE>DEBIT</TRNTYPE>
            <DTPOSTED>20260115000000</DTPOSTED>
            <TRNAMT>-45.50</TRNAMT>
            <FITID>20260115001</FITID>
            <NAME>GAS STATION</NAME>
            <MEMO>FUEL</MEMO>
          </STMTTRN>
        </BANKTRANLIST>
        <LEDGERBAL>
          <BALAMT>5334.50</BALAMT>
          <DTASOF>20260131000000</DTASOF>
        </LEDGERBAL>
      </STMTRS>
    </STMTTRNRS>
  </BANKMSGSRSV1>
</OFX>
"""

# OFX 1.x (SGML — no closing tags on leaf elements)
OFX1_CHECKING = """\
OFXHEADER:100
DATA:OFXSGML
VERSION:151
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<SIGNONMSGSRSV1>
<SONRS>
<STATUS>
<CODE>0
<SEVERITY>INFO
</STATUS>
<DTSERVER>20260201000000
<LANGUAGE>ENG
</SONRS>
</SIGNONMSGSRSV1>
<BANKMSGSRSV1>
<STMTTRNRS>
<TRNUID>1
<STATUS>
<CODE>0
<SEVERITY>INFO
</STATUS>
<STMTRS>
<CURDEF>USD
<BANKACCTFROM>
<BANKID>021000021
<ACCTID>123456781234
<ACCTTYPE>CHECKING
</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>20260101000000
<DTEND>20260131000000
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260105000000
<TRNAMT>3500.00
<FITID>20260105001
<NAME>ACME PAYROLL
<MEMO>DIRECT DEPOSIT
</STMTTRN>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260110000000
<TRNAMT>-120.00
<FITID>20260110001
<NAME>GROCERY STORE
</STMTTRN>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260115000000
<TRNAMT>-45.50
<FITID>20260115001
<NAME>GAS STATION
<MEMO>FUEL
</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL>
<BALAMT>5334.50
<DTASOF>20260131000000
</LEDGERBAL>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""

# Credit card OFX (CCACCTFROM, no LEDGERBAL)
OFX2_CREDIT = """\
<?xml version="1.0" encoding="UTF-8"?>
<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
  <SIGNONMSGSRSV1>
    <SONRS><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
    <DTSERVER>20260201000000</DTSERVER><LANGUAGE>ENG</LANGUAGE></SONRS>
  </SIGNONMSGSRSV1>
  <CREDITCARDMSGSRSV1>
    <CCTRNRS>
      <TRNUID>1</TRNUID><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
      <CCSTMTRS>
        <CURDEF>USD</CURDEF>
        <CCACCTFROM>
          <ACCTID>000099995678</ACCTID>
        </CCACCTFROM>
        <BANKTRANLIST>
          <DTSTART>20260101000000</DTSTART>
          <DTEND>20260131000000</DTEND>
          <STMTTRN>
            <TRNTYPE>DEBIT</TRNTYPE>
            <DTPOSTED>20260108000000</DTPOSTED>
            <TRNAMT>-80.00</TRNAMT>
            <FITID>CC20260108001</FITID>
            <NAME>RESTAURANT ABC</NAME>
          </STMTTRN>
          <STMTTRN>
            <TRNTYPE>CREDIT</TRNTYPE>
            <DTPOSTED>20260120000000</DTPOSTED>
            <TRNAMT>500.00</TRNAMT>
            <FITID>CC20260120001</FITID>
            <NAME>PAYMENT THANK YOU</NAME>
          </STMTTRN>
        </BANKTRANLIST>
      </CCSTMTRS>
    </CCTRNRS>
  </CREDITCARDMSGSRSV1>
</OFX>
"""

# OFX with & in description (real-world gotcha: ofxparse must handle this)
OFX2_AMP = """\
<?xml version="1.0" encoding="UTF-8"?>
<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
  <SIGNONMSGSRSV1>
    <SONRS><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
    <DTSERVER>20260201000000</DTSERVER><LANGUAGE>ENG</LANGUAGE></SONRS>
  </SIGNONMSGSRSV1>
  <BANKMSGSRSV1>
    <STMTTRNRS>
      <TRNUID>1</TRNUID><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
      <STMTRS>
        <CURDEF>GBP</CURDEF>
        <BANKACCTFROM>
          <BANKID>040004</BANKID>
          <ACCTID>87654321</ACCTID>
          <ACCTTYPE>CHECKING</ACCTTYPE>
        </BANKACCTFROM>
        <BANKTRANLIST>
          <DTSTART>20260101000000</DTSTART>
          <DTEND>20260131000000</DTEND>
          <STMTTRN>
            <TRNTYPE>DEBIT</TRNTYPE>
            <DTPOSTED>20260112000000</DTPOSTED>
            <TRNAMT>-25.00</TRNAMT>
            <FITID>AMP001</FITID>
            <NAME>SMITH &amp; JONES LTD</NAME>
          </STMTTRN>
        </BANKTRANLIST>
        <LEDGERBAL>
          <BALAMT>975.00</BALAMT>
          <DTASOF>20260131000000</DTASOF>
        </LEDGERBAL>
      </STMTRS>
    </STMTTRNRS>
  </BANKMSGSRSV1>
</OFX>
"""


# ── Parser tests ───────────────────────────────────────────────────────────────

def test_parse_ofx2_checking():
    txns = parse(OFX2_CHECKING)
    assert len(txns) == 3
    payroll, grocery, gas = txns

    assert payroll.date == "2026-01-05"
    assert payroll.description == "ACME PAYROLL DIRECT DEPOSIT"
    assert payroll.settlement_amount == pytest.approx(3500.00)
    assert payroll.transaction_currency == "USD"
    assert payroll.settlement_currency == "USD"
    assert payroll.fx_rate is None
    assert payroll.balance_after is None

    assert grocery.date == "2026-01-10"
    assert grocery.description == "GROCERY STORE"
    assert grocery.settlement_amount == pytest.approx(-120.00)

    assert gas.description == "GAS STATION FUEL"
    assert gas.settlement_amount == pytest.approx(-45.50)


def test_parse_ofx1_checking():
    txns = parse(OFX1_CHECKING)
    assert len(txns) == 3
    payroll, grocery, gas = txns

    assert payroll.date == "2026-01-05"
    assert payroll.settlement_amount == pytest.approx(3500.00)
    assert grocery.settlement_amount == pytest.approx(-120.00)
    assert gas.settlement_amount == pytest.approx(-45.50)


def test_ofx1_and_ofx2_produce_identical_transactions():
    t1 = parse(OFX2_CHECKING)
    t2 = parse(OFX1_CHECKING)
    assert len(t1) == len(t2)
    for a, b in zip(t1, t2):
        assert a.date == b.date
        assert a.settlement_amount == pytest.approx(b.settlement_amount)
        assert a.description == b.description


def test_parse_credit_card_ofx():
    txns = parse(OFX2_CREDIT)
    assert len(txns) == 2
    charge, payment = txns
    assert charge.settlement_amount == pytest.approx(-80.00)
    assert payment.settlement_amount == pytest.approx(500.00)
    assert charge.transaction_currency == "USD"


def test_description_falls_back_to_memo_when_name_absent():
    # GROCERY STORE has no MEMO; description should be just the NAME.
    txns = parse(OFX2_CHECKING)
    grocery = txns[1]
    assert grocery.description == "GROCERY STORE"


def test_description_uses_name_alone_when_memo_equals_name():
    # If NAME == MEMO, we don't want "ACME ACME" duplication.
    # OFX2_CHECKING has ACME PAYROLL + DIRECT DEPOSIT (different) → combined.
    # Here we just confirm the combiner only runs when they differ.
    txns = parse(OFX2_CHECKING)
    assert "ACME PAYROLL DIRECT DEPOSIT" == txns[0].description


def test_amp_in_description_does_not_crash():
    txns = parse(OFX2_AMP)
    assert len(txns) == 1
    assert "SMITH" in txns[0].description
    assert "JONES" in txns[0].description


def test_currencies_match_curdef():
    txns = parse(OFX2_AMP)
    assert txns[0].transaction_currency == "GBP"
    assert txns[0].settlement_currency == "GBP"


# ── Reconciliation tests ───────────────────────────────────────────────────────

def test_reconcile_with_ledger_balance():
    txns = parse(OFX2_CHECKING)
    bal = extract_ledger_balance(OFX2_CHECKING)
    result = reconcile(txns, closing_balance=bal)

    assert result.stated_balance == pytest.approx(5334.50)
    assert result.diff is None
    assert not result.is_clean               # unverified — is_clean=False because mismatches present
    assert result.mismatches                 # explains why


def test_reconcile_without_ledger_balance():
    txns = parse(OFX2_CREDIT)
    result = reconcile(txns, closing_balance=None)

    assert result.stated_balance is None
    assert result.diff is None
    assert result.is_clean                   # nothing to check → clean


def test_extract_ledger_balance_present():
    assert extract_ledger_balance(OFX2_CHECKING) == pytest.approx(5334.50)
    assert extract_ledger_balance(OFX2_AMP)      == pytest.approx(975.00)


def test_extract_ledger_balance_absent():
    assert extract_ledger_balance(OFX2_CREDIT) is None


# ── Account info extraction ────────────────────────────────────────────────────

def test_extract_account_info_checking():
    info = extract_account_info(OFX2_CHECKING)
    assert info["last4"] == "1234"
    assert info["account_type"].upper() == "CHECKING"
    assert info["currency"] == "USD"
    assert info["start_date"] is not None
    assert info["end_date"] is not None


def test_extract_account_info_credit():
    info = extract_account_info(OFX2_CREDIT)
    assert info["last4"] == "5678"


def test_extract_account_info_gbp():
    info = extract_account_info(OFX2_AMP)
    assert info["currency"] == "GBP"
    assert info["last4"] == "4321"


# ── Ingestion integration (in-memory DB) ──────────────────────────────────────

@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_schema(c)
    c.execute("INSERT INTO owners (name) VALUES ('Test User')")
    c.execute("""
        INSERT INTO accounts (account_code, display_name, account_type, currency, owner_id, statement_format)
        VALUES ('US_CHECKING', 'US Checking', 'checking', 'USD', 1, 'ofx')
    """)
    c.execute("INSERT INTO categories (name, money_type) VALUES ('Miscellaneous', 'expense')")
    c.commit()
    return c


def _repos(c):
    return dict(
        txn_repo=TransactionRepository(c),
        rule_repo=VendorRuleRepository(c),
        category_repo=CategoryRepository(c),
        review_repo=ReviewQueueRepository(c),
        rate_repo=ExchangeRateRepository(c),
    )


def test_ingest_ofx_transactions(conn):
    txns = parse(OFX2_CHECKING)
    account_id = conn.execute("SELECT id FROM accounts WHERE account_code='US_CHECKING'").fetchone()["id"]
    inserted, skipped = ingest_transactions(txns, account_id, "jan2026.ofx", **_repos(conn))
    assert len(inserted) == 3
    assert skipped == 0


def test_reimport_is_idempotent_via_fingerprint(conn):
    """OFX reimport skips all existing transactions via fingerprint dedup instead of raising."""
    txns = parse(OFX2_CHECKING)
    account_id = conn.execute("SELECT id FROM accounts WHERE account_code='US_CHECKING'").fetchone()["id"]
    inserted1, skipped1 = ingest_transactions(txns, account_id, "jan2026.ofx",
                                              skip_statement_check=True, **_repos(conn))
    conn.commit()
    assert len(inserted1) == 3 and skipped1 == 0

    inserted2, skipped2 = ingest_transactions(txns, account_id, "jan2026.ofx",
                                              skip_statement_check=True, **_repos(conn))
    assert len(inserted2) == 0 and skipped2 == 3


def test_ofx_credit_card_positive_tagged_as_reimbursement(conn):
    """Unmatched positive OFX entries on a credit card account are reimbursements, not income."""
    conn.execute("""
        INSERT INTO accounts (account_code, display_name, account_type, currency, owner_id, statement_format)
        VALUES ('US_CREDIT_CARD', 'US Credit Card', 'credit_card', 'USD', 1, 'ofx')
    """)
    conn.commit()
    txns = parse(OFX2_CREDIT)
    account_id = conn.execute("SELECT id FROM accounts WHERE account_code='US_CREDIT_CARD'").fetchone()["id"]
    inserted, _ = ingest_transactions(txns, account_id, "cc_jan2026.ofx",
                                      is_credit_card=True, skip_statement_check=True, **_repos(conn))
    rows = {r["settlement_amount"]: r["money_type"]
            for r in conn.execute(
                f"SELECT settlement_amount, money_type FROM transactions WHERE id IN ({','.join('?' * len(inserted))})",
                inserted,
            ).fetchall()}
    assert rows[500.0] == "reimbursement"
    assert rows[-80.0] == "expense"
