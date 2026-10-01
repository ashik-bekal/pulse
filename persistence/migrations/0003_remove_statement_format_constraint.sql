-- Make statement formats extensible. Parser availability is validated by the
-- application; the database stores the stable format name without maintaining
-- a closed list that would require a table rebuild for every new parser.
-- SQLite cannot remove the existing CHECK in place, so rebuild this table once.
PRAGMA foreign_keys = OFF;

CREATE TABLE accounts_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_code TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    description TEXT,
    account_type TEXT NOT NULL CHECK(account_type IN ('checking','savings','credit_card','investment')),
    institution TEXT,
    account_number_last4 TEXT,
    currency TEXT NOT NULL,
    owner_id INTEGER NOT NULL REFERENCES owners(id),
    opening_balance_native REAL,
    opened_date TEXT,
    closed_date TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    statement_format TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

INSERT INTO accounts_new (
    id, account_code, display_name, description, account_type, institution,
    account_number_last4, currency, owner_id, opening_balance_native,
    opened_date, closed_date, is_active, statement_format, created_at
)
SELECT
    id, account_code, display_name, description, account_type, institution,
    account_number_last4, currency, owner_id, opening_balance_native,
    opened_date, closed_date, is_active, statement_format, created_at
FROM accounts;

DROP TABLE accounts;
ALTER TABLE accounts_new RENAME TO accounts;

PRAGMA foreign_keys = ON;
PRAGMA foreign_key_check;
