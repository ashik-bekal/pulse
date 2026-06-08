"""
Initialize the PULSE database: create schema and seed a starter chart
of accounts, demo owner/accounts, sample vendor rules, and exchange rates.

All seed data here is FICTIONAL — placeholder owner, made-up account
numbers, and generic vendor patterns. Replace or extend via the web UI
(Accounts / Categories / Vendor Rules pages) after first run.

Run: python3 cli/init_db.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from persistence.database import get_connection, init_schema

DEMO_OWNER = "Demo User"


def seed_owners(conn):
    conn.execute("INSERT OR IGNORE INTO owners (name) VALUES (?)", (DEMO_OWNER,))


def seed_accounts(conn):
    owner_id = conn.execute("SELECT id FROM owners WHERE name = ?", (DEMO_OWNER,)).fetchone()["id"]
    accounts = [
        ("UK_CURRENT", "UK Current Account", "GBP current account (demo)", "checking",
         "Example Bank UK", "0001", "GBP", owner_id, "hsbc"),
        ("US_CHECKING", "US Checking", "USD checking (demo)", "checking",
         "Example Bank US", "0002", "USD", owner_id, "chase_bank"),
        ("US_SAVINGS", "US Savings", "USD savings (demo)", "savings",
         "Example Bank US", "0003", "USD", owner_id, "chase_bank"),
        ("US_CREDIT_CARD", "US Credit Card", "USD credit card (demo)", "credit_card",
         "Example Bank US", "0004", "USD", owner_id, "sapphire"),
    ]
    for code, name, desc, atype, inst, last4, ccy, owner, fmt in accounts:
        conn.execute("""
            INSERT OR IGNORE INTO accounts
                (account_code, display_name, description, account_type, institution,
                 account_number_last4, currency, owner_id, statement_format)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (code, name, desc, atype, inst, last4, ccy, owner, fmt))


def seed_categories(conn):
    top_level = [
        ("Income", "income"), ("Reimbursement", "reimbursement"), ("Loan Repayment", "loan_repayment"),
        ("Housing", "expense"), ("Transport", "expense"), ("Groceries", "expense"),
        ("Eating Out & Takeaway", "expense"), ("Health", "expense"), ("Personal & Household", "expense"),
        ("Fees & Charges", "expense"), ("Leisure & Entertainment", "expense"), ("Gifts & Donations", "expense"),
        ("Pet", "expense"), ("Utilities", "expense"), ("Subscriptions", "expense"),
        ("Travel", "expense"), ("Miscellaneous", "expense"), ("Transfer", "transfer"),
    ]
    name_to_id = {}
    for name, mtype in top_level:
        # SQLite treats NULL != NULL in UNIQUE constraints, so INSERT OR IGNORE won't
        # deduplicate rows with parent_id IS NULL. Check existence explicitly instead.
        existing = conn.execute(
            "SELECT id FROM categories WHERE name=? AND parent_id IS NULL", (name,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO categories (name, parent_id, money_type) VALUES (?, NULL, ?)", (name, mtype)
            )
            existing = conn.execute(
                "SELECT id FROM categories WHERE name=? AND parent_id IS NULL", (name,)
            ).fetchone()
        name_to_id[name] = existing["id"]

    sub_categories = [
        ("Income - Salary", "Income", "income"),
        ("Reimbursement - Expense Split", "Reimbursement", "reimbursement"),
        ("Transport - Other", "Transport", "expense"),
        ("Transport - Commute", "Transport", "expense"),
        ("Housing - Rent/Deposit", "Housing", "expense"),
        ("Housing - Council Tax", "Housing", "expense"),
        ("Utilities - Energy", "Utilities", "expense"),
        ("Utilities - Broadband", "Utilities", "expense"),
        ("Utilities - Water", "Utilities", "expense"),
    ]
    for name, parent, mtype in sub_categories:
        if not conn.execute(
            "SELECT 1 FROM categories WHERE name=? AND parent_id=?", (name, name_to_id[parent])
        ).fetchone():
            conn.execute(
                "INSERT INTO categories (name, parent_id, money_type) VALUES (?, ?, ?)",
                (name, name_to_id[parent], mtype)
            )


def seed_exchange_rates(conn):
    months = [f"2025-{m:02d}" for m in range(1, 13)] + [f"2026-{m:02d}" for m in range(1, 13)]
    for ym in months:
        conn.execute("INSERT OR IGNORE INTO exchange_rates (year_month, currency, rate_to_reporting) VALUES (?, 'USD', 0.74)", (ym,))
        conn.execute("INSERT OR IGNORE INTO exchange_rates (year_month, currency, rate_to_reporting) VALUES (?, 'EUR', 0.87)", (ym,))


def seed_vendor_rules(conn):
    """A few generic starter rules demonstrating each match behavior.
    Users add their own via the Vendor Rules page — including auto-suggested
    ones the app proposes after consistent manual categorization."""
    def cat_id(name):
        row = conn.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
        return row["id"] if row else None

    owner_id = conn.execute("SELECT id FROM owners WHERE name=?", (DEMO_OWNER,)).fetchone()["id"]

    rules = [
        ("ACME PAYROLL", "Income - Salary", "income", owner_id, "high",
         "Example: employer payroll credit"),
        ("CITY POWER CO", "Utilities - Energy", "expense", owner_id, "high",
         "Example: energy supplier direct debit"),
        ("METRO BROADBAND", "Utilities - Broadband", "expense", owner_id, "high", None),
        ("CORNER GROCER", "Groceries", "expense", owner_id, "high", None),
        ("RIDESHARE", "Transport - Commute", "expense", owner_id, "medium",
         "Example of a medium-confidence rule: matches land in the review queue"),
        ("SELF TRANSFER", "Transfer", "transfer", owner_id, "high",
         "Example: transfers between your own accounts, excluded from spend totals"),
    ]
    for pattern, cat_name, mtype, owner, confidence, notes in rules:
        conn.execute("""
            INSERT OR IGNORE INTO vendor_rules
                (pattern, match_type, category_id, money_type, owner_id, confidence, notes)
            VALUES (?, 'contains', ?, ?, ?, ?, ?)
        """, (pattern, cat_id(cat_name), mtype, owner, confidence, notes))


def seed_trips(conn):
    for name, start, end in [
        ("Sample Beach Trip", "2025-06-06", "2025-06-09"),
        ("Sample City Break", "2025-09-12", "2025-09-15"),
    ]:
        if not conn.execute("SELECT 1 FROM trips WHERE name=?", (name,)).fetchone():
            conn.execute(
                "INSERT INTO trips (name, start_date, end_date) VALUES (?, ?, ?)",
                (name, start, end)
            )


def main():
    from persistence.database import get_db_path
    os.makedirs(os.path.dirname(get_db_path()), exist_ok=True)

    conn = get_connection()
    init_schema(conn)
    seed_owners(conn)
    seed_accounts(conn)
    seed_categories(conn)
    seed_exchange_rates(conn)
    seed_vendor_rules(conn)
    seed_trips(conn)
    conn.commit()

    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ["owners", "accounts", "categories", "vendor_rules", "trips", "exchange_rates"]}
    print("Database initialized.")
    for k, v in counts.items():
        print(f"  {k}: {v} rows")
    conn.close()


if __name__ == "__main__":
    main()
