"""
Seed PULSE with a fictional demo dataset so the app is explorable
immediately — no real statements required.

Creates four months (Jun–Sep 2025) of invented activity across the demo
accounts: salaries, rent, utilities, groceries, an FX purchase, a
credit-card cycle with a payment, a tagged trip, reconciled monthly
snapshots, and one review-queue item.

Everything here is fabricated. Safe to run repeatedly (idempotent-ish:
it skips seeding if demo transactions already exist).

Run: python3 cli/seed_demo.py     (runs cli/init_db.py implicitly)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from persistence.database import get_connection
import init_db as base_seed  # noqa: E402  (same directory)

USD_RATE = 0.74  # matches the seeded exchange_rates


def cat(conn, name):
    return conn.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()["id"]


def acct(conn, code):
    return conn.execute("SELECT id FROM accounts WHERE account_code=?", (code,)).fetchone()["id"]


def add_txn(conn, account_id, date, desc, amount, currency, category, money_type,
            confidence="high", trip_id=None, fx=None):
    """fx = (native_currency, native_amount, rate) for cross-currency rows."""
    native_ccy, native_amt = currency, amount
    fx_rate = None
    if fx:
        native_ccy, native_amt, fx_rate = fx
    reporting = amount if currency == "GBP" else round(amount * USD_RATE, 2)
    cur = conn.execute("""
        INSERT INTO transactions (account_id, transaction_date, description,
            transaction_currency, transaction_amount, settlement_currency, settlement_amount,
            fx_rate, reporting_amount, category_id, money_type, trip_id, confidence,
            source_statement)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'demo-seed')
    """, (account_id, date, desc, native_ccy, native_amt, currency, amount,
          fx_rate, reporting, cat(conn, category), money_type, trip_id, confidence))
    return cur.lastrowid


def main():
    base_seed.main()  # schema + base seeds (idempotent)

    conn = get_connection()
    if conn.execute("SELECT 1 FROM transactions WHERE source_statement='demo-seed' LIMIT 1").fetchone():
        print("Demo data already present — nothing to do.")
        conn.close()
        return

    uk = acct(conn, "UK_CURRENT")
    us = acct(conn, "US_CHECKING")
    cc = acct(conn, "US_CREDIT_CARD")
    trip_id = conn.execute("SELECT id FROM trips WHERE name='Sample Beach Trip'").fetchone()["id"]

    for i, month in enumerate(["2025-06", "2025-07", "2025-08", "2025-09"]):
        # UK current account — a typical month
        add_txn(conn, uk, f"{month}-25", "ACME PAYROLL", 2800.00, "GBP", "Income - Salary", "income")
        add_txn(conn, uk, f"{month}-01", "CITY LETTINGS RENT", -950.00, "GBP", "Housing - Rent/Deposit", "expense")
        add_txn(conn, uk, f"{month}-03", "CITY POWER CO", -62.40, "GBP", "Utilities - Energy", "expense")
        add_txn(conn, uk, f"{month}-05", "METRO BROADBAND", -30.00, "GBP", "Utilities - Broadband", "expense")
        for day in ("04", "12", "20"):
            add_txn(conn, uk, f"{month}-{day}", "CORNER GROCER", -43.75, "GBP", "Groceries", "expense")
        add_txn(conn, uk, f"{month}-14", "RIVERSIDE CAFE", -18.20, "GBP", "Eating Out & Takeaway", "expense")
        add_txn(conn, uk, f"{month}-28", "SELF TRANSFER TO SAVINGS", -200.00, "GBP", "Transfer", "transfer")

        # US checking
        add_txn(conn, us, f"{month}-15", "ACME PAYROLL US", 3200.00, "USD", "Income - Salary", "income")
        add_txn(conn, us, f"{month}-02", "PARKSIDE APARTMENTS", -1400.00, "USD", "Housing - Rent/Deposit", "expense")
        add_txn(conn, us, f"{month}-18", "GREEN MARKET", -85.10, "USD", "Groceries", "expense")

        # Credit card cycle: purchases + payment
        add_txn(conn, cc, f"{month}-07", "STREAMFLIX SUBSCRIPTION", -14.99, "USD", "Subscriptions", "expense")
        add_txn(conn, cc, f"{month}-11", "NOODLE HOUSE", -27.60, "USD", "Eating Out & Takeaway", "expense")
        add_txn(conn, cc, f"{month}-21", "PAYMENT THANK YOU", 42.59, "USD", "Transfer", "transfer")

    # Trip spending (Jun 6-9), tagged to the sample trip — includes one FX row
    add_txn(conn, cc, "2025-06-06", "SEASIDE HOTEL", -240.00, "USD", "Travel", "expense", trip_id=trip_id)
    add_txn(conn, cc, "2025-06-07", "BOARDWALK BISTRO LONDON", -27.18, "USD", "Travel", "expense",
            trip_id=trip_id, fx=("GBP", -20.00, 1.359))
    add_txn(conn, cc, "2025-06-08", "COASTAL RAIL", -18.50, "USD", "Travel", "expense", trip_id=trip_id)
    add_txn(conn, uk, "2025-06-09", "AIRPORT COACH", -24.00, "GBP", "Travel", "expense", trip_id=trip_id)

    # One low-confidence item so the review queue has something to show
    txn_id = add_txn(conn, uk, "2025-09-22", "UNKNOWN VENDOR 4821", -12.34, "GBP",
                     "Miscellaneous", "expense", confidence="low")
    conn.execute("""
        INSERT INTO review_queue (transaction_id, issue_description, severity)
        VALUES (?, 'Auto-categorized with low confidence (no vendor rule matched)', 'low')
    """, (txn_id,))

    # Reconciled month-end snapshots (fictional balances, exact tie-out)
    snapshots = {
        uk: [("2025-06", 3211.90), ("2025-07", 4623.80), ("2025-08", 6035.70), ("2025-09", 7435.26)],
        us: [("2025-06", 4914.90), ("2025-07", 6629.80), ("2025-08", 8344.70), ("2025-09", 10059.60)],
        cc: [("2025-06", 328.27), ("2025-07", 42.59), ("2025-08", 42.59), ("2025-09", 42.59)],
    }
    for account_id, rows in snapshots.items():
        for ym, closing in rows:
            conn.execute("""
                INSERT OR REPLACE INTO account_snapshots
                    (account_id, year_month, closing_balance_native, statement_closing_balance,
                     reconciled, reconciliation_diff)
                VALUES (?, ?, ?, ?, 1, 0.0)
            """, (account_id, ym, closing, closing))

    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM transactions WHERE source_statement='demo-seed'").fetchone()[0]
    print(f"Demo dataset seeded: {n} transactions across 3 accounts, "
          f"1 trip, 12 reconciled snapshots, 1 review item.")
    conn.close()


if __name__ == "__main__":
    main()
