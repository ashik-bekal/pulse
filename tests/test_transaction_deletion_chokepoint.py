"""
Enforces the invariant: a transaction row may only ever be removed by
TransactionRepository.delete_period() or .delete_all() — never as a side
effect of deleting a category, trip, or vendor rule, and never via inline
SQL anywhere else. Losing a transaction must always be a deliberate,
whole-period decision.

If this test fails because you added a legitimate new deletion path, add
it as a named method on TransactionRepository (next to delete_period/
delete_all) and extend ALLOWED_LINES below — don't just delete transactions
inline and widen the grep.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")

# (relative file path, substring of the line) pairs that are allowed to
# contain "DELETE FROM transactions" — everywhere else in the codebase, it
# must not appear.
ALLOWED = [
    ("persistence/repositories.py", 'DELETE FROM transactions WHERE id IN'),
    ("persistence/repositories.py", 'DELETE FROM transactions")'),
    ("persistence/repositories.py", 'no other "DELETE FROM transactions"'),  # this file's own docstring
]

SCAN_DIRS = ["web", "cli", "persistence", "services", "domain"]
PATTERN = re.compile(r"DELETE\s+FROM\s+transactions\b", re.IGNORECASE)


def _iter_py_files():
    for d in SCAN_DIRS:
        base = os.path.join(REPO_ROOT, d)
        for root, _, files in os.walk(base):
            for f in files:
                if f.endswith(".py"):
                    yield os.path.join(root, f)


def test_transactions_table_only_deleted_via_named_repository_methods():
    violations = []
    for path in _iter_py_files():
        rel = os.path.relpath(path, REPO_ROOT)
        with open(path) as fh:
            for lineno, line in enumerate(fh, start=1):
                if not PATTERN.search(line):
                    continue
                if any(rel == allowed_path and snippet in line for allowed_path, snippet in ALLOWED):
                    continue
                violations.append(f"{rel}:{lineno}: {line.strip()}")

    assert not violations, (
        "Found DELETE FROM transactions outside the sanctioned chokepoint "
        "(TransactionRepository.delete_period/delete_all):\n" + "\n".join(violations)
    )


def test_category_delete_does_not_touch_transactions():
    from persistence.repositories import CategoryRepository
    import sqlite3
    from persistence.database import init_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    conn.execute("INSERT INTO owners (name) VALUES ('Demo')")
    conn.execute("""
        INSERT INTO accounts (account_code, display_name, account_type, currency, owner_id)
        VALUES ('ACC', 'Acc', 'checking', 'GBP', 1)
    """)
    conn.execute("INSERT INTO categories (name, money_type) VALUES ('Groceries', 'expense')")
    conn.execute("""
        INSERT INTO transactions (account_id, transaction_date, description,
            transaction_currency, transaction_amount, settlement_currency, settlement_amount,
            reporting_amount, category_id, money_type, confidence)
        VALUES (1, '2025-01-01', 'Shop', 'GBP', -10.0, 'GBP', -10.0, -10.0, 1, 'expense', 'high')
    """)
    conn.commit()

    result = CategoryRepository(conn).delete(1)
    assert result == "in_use"  # refused, not silently orphaning the transaction
    remaining = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert remaining == 1  # transaction untouched
