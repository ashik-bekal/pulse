-- Master Ledger Database Schema
-- Implements the design agreed in planning: multi-currency, multi-owner,
-- trip-as-entity, category hierarchy, vendor rules engine, audit trail.

PRAGMA foreign_keys = ON;

-- ============================================================
-- OWNERS
-- ============================================================
CREATE TABLE IF NOT EXISTS owners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT DEFAULT (datetime('now'))
);

-- ============================================================
-- ACCOUNTS
-- account_type: checking, savings, credit_card, investment
-- ============================================================
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_code TEXT NOT NULL UNIQUE,      -- short stable id, e.g. 'HSBC_UK', 'CHASE_CHECKING'
    display_name TEXT NOT NULL,
    description TEXT,
    account_type TEXT NOT NULL CHECK(account_type IN ('checking','savings','credit_card','investment')),
    institution TEXT,                        -- 'HSBC UK', 'Chase'
    account_number_last4 TEXT,
    currency TEXT NOT NULL,                  -- native currency of the account itself, e.g. GBP, USD
    owner_id INTEGER NOT NULL REFERENCES owners(id),
    opening_balance_native REAL,             -- known opening balance (e.g. £0 for new accounts); used when first statement has no BALANCE BROUGHT FORWARD
    opened_date TEXT,
    closed_date TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    statement_format TEXT CHECK(statement_format IN ('hsbc','chase_bank','sapphire','ofx') OR statement_format IS NULL),
    -- which PDF parser this account's statements use, if any. When more than
    -- one account shares a format (e.g. two people's Chase Checking
    -- accounts), the import screen shows a target-account picker instead of
    -- silently importing into whichever account happens to be hardcoded.
    created_at TEXT DEFAULT (datetime('now'))
);

-- ============================================================
-- CATEGORIES (Chart of Accounts, hierarchical)
-- ============================================================
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                      -- e.g. "Eating Out", "Transport - Other"
    parent_id INTEGER REFERENCES categories(id),
    money_type TEXT CHECK(money_type IN ('income','reimbursement','loan_repayment','expense','transfer')),
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(name, parent_id)
);

-- ============================================================
-- TRIPS (first-class entity, not a string tag)
-- ============================================================
CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,               -- e.g. "Greece (Sep)", "India (Jan)"
    start_date TEXT,
    end_date TEXT,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trip_owners (
    trip_id INTEGER NOT NULL REFERENCES trips(id),
    owner_id INTEGER NOT NULL REFERENCES owners(id),
    PRIMARY KEY (trip_id, owner_id)
);

-- ============================================================
-- EXCHANGE RATES (monthly, not hardcoded)
-- rate_to_reporting: multiply native amount by this to get reporting currency
-- ============================================================
CREATE TABLE IF NOT EXISTS exchange_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year_month TEXT NOT NULL,                -- 'YYYY-MM'
    currency TEXT NOT NULL,                  -- 'USD', 'EUR', etc (GBP excluded - it's the reporting currency)
    rate_to_reporting REAL NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(year_month, currency)
);

-- ============================================================
-- VENDOR RULES (the categorization rules engine)
-- pattern matched against transaction description, case-insensitive substring by default
-- ============================================================
CREATE TABLE IF NOT EXISTS vendor_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,                   -- e.g. 'ACME PAYROLL', 'CITY POWER CO'
    match_type TEXT NOT NULL DEFAULT 'contains' CHECK(match_type IN ('contains','exact','regex')),
    category_id INTEGER REFERENCES categories(id),
    money_type TEXT CHECK(money_type IN ('income','reimbursement','loan_repayment','expense','transfer')),
    owner_id INTEGER REFERENCES owners(id),
    confidence TEXT NOT NULL DEFAULT 'high' CHECK(confidence IN ('high','medium','low')),
    notes TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    is_pending INTEGER NOT NULL DEFAULT 0,   -- 1 = auto-suggested, awaiting user approval before categorize() will use it
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- ============================================================
-- TRANSACTIONS
-- Two currency pairs per the HSBC/Sapphire FX discussion:
--   transaction_currency/amount = currency the purchase actually happened in
--   settlement_currency/amount  = currency that hit the account balance
-- For domestic transactions these are identical.
-- ============================================================
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    transaction_date TEXT NOT NULL,          -- YYYY-MM-DD, anchors calendar-month reporting
    posted_date TEXT,                        -- if different from transaction_date (e.g. Sapphire posting lag)
    description TEXT NOT NULL,
    raw_description TEXT,                    -- original unprocessed text, for audit/debugging

    transaction_currency TEXT NOT NULL,
    transaction_amount REAL NOT NULL,         -- signed: negative = outflow, positive = inflow
    settlement_currency TEXT NOT NULL,
    settlement_amount REAL NOT NULL,          -- signed, same convention
    fx_rate REAL,                             -- null for non-FX transactions

    reporting_amount REAL,                    -- computed: settlement_amount converted to reporting currency
                                               -- using exchange_rates for that month (or 1.0 if already reporting ccy)

    category_id INTEGER REFERENCES categories(id),
    money_type TEXT NOT NULL CHECK(money_type IN ('income','reimbursement','loan_repayment','expense','transfer')),
    owner_id INTEGER REFERENCES owners(id),   -- whose spend/income this is (may differ from account owner for splits)
    trip_id INTEGER REFERENCES trips(id),

    balance_after REAL,                       -- statement-printed running balance, for reconciliation only
    confidence TEXT NOT NULL DEFAULT 'high' CHECK(confidence IN ('high','medium','low')),
    matched_rule_id INTEGER REFERENCES vendor_rules(id),  -- which rule (if any) auto-categorized this
    notes TEXT,

    source_statement TEXT,                    -- filename of source PDF, for traceability
    is_hidden INTEGER NOT NULL DEFAULT 0,      -- excluded from all displayed views/reports without deleting the row
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_transactions_account_date ON transactions(account_id, transaction_date);
CREATE INDEX IF NOT EXISTS idx_transactions_trip ON transactions(trip_id);
CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category_id);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(transaction_date);
CREATE INDEX IF NOT EXISTS idx_transactions_source ON transactions(account_id, source_statement);

-- ============================================================
-- ACCOUNT SNAPSHOTS (end-of-month closing balance per account, native currency)
-- This is the stored fact for multi-currency position tracking - not derived
-- from summing transactions, so it can't silently drift.
-- ============================================================
CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    year_month TEXT NOT NULL,                -- 'YYYY-MM'
    closing_balance_native REAL NOT NULL,
    statement_closing_balance REAL,           -- as printed on statement, for validation
    reconciled INTEGER NOT NULL DEFAULT 0,    -- 1 if tied out exactly
    reconciliation_diff REAL,                 -- computed - statement, should be 0 when reconciled
    notes TEXT,
    is_hidden INTEGER NOT NULL DEFAULT 0,     -- excluded from coverage/net-worth views without deleting the row
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(account_id, year_month)
);

-- ============================================================
-- RECURRING COMMITMENTS (with variance alerting fields)
-- ============================================================
CREATE TABLE IF NOT EXISTS recurring_commitments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_pattern TEXT NOT NULL,             -- matches against transaction description
    display_name TEXT NOT NULL,
    category_id INTEGER REFERENCES categories(id),
    account_id INTEGER REFERENCES accounts(id),
    expected_amount_min REAL,
    expected_amount_max REAL,
    expected_day_of_month INTEGER,            -- approximate, +/- a few days tolerance
    cadence TEXT NOT NULL DEFAULT 'monthly' CHECK(cadence IN ('monthly','quarterly','annual')),
    is_active INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- ============================================================
-- AUDIT LOG (every retroactive change, who/what/when/why)
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,                -- 'transaction', 'vendor_rule', 'category', etc.
    entity_id INTEGER NOT NULL,
    field_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    changed_at TEXT DEFAULT (datetime('now'))
);

-- ============================================================
-- LOW CONFIDENCE REVIEW QUEUE (replaces the Excel tab)
-- Just a view over transactions/rules flagged low/medium confidence,
-- but kept as an explicit table so resolved items can be removed cleanly
-- with their resolution recorded.
-- ============================================================
CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER REFERENCES transactions(id),
    issue_description TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'low' CHECK(severity IN ('low','medium','high')),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved','wont_fix')),
    resolution_notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_queue(status);
