"""
Repositories — the ONLY place SQL strings live in this codebase.

PREVIOUS DESIGN PROBLEM: SQL was scattered as inline strings across
app/main.py (Flask routes) and scripts/ingest_statement.py, with no single
place owning the read/write contract for a table. Adding a column meant
grepping the whole codebase for every place that touched that table.

FIX: one repository class per aggregate (accounts, categories, vendor
rules, transactions, trips, snapshots, review queue). Callers — services
and web routes — depend on these classes, never on raw SQL or a bare
sqlite3.Connection beyond passing it in. This is the one deliberately
"clean architecture" pattern adopted here, and it earns its place: it's
the difference between "where do I add a column" being a one-file answer
or a grep-the-whole-repo answer.

Each repository takes a connection in its constructor rather than opening
its own — this is what makes services.ingestion testable with a single
shared in-memory connection instead of hitting disk, and is what fixed the
N+1 query problem in the original categorizer (which opened a fresh
connection per call).
"""
import sqlite3
from typing import List, Optional

from domain.models import RawTransaction, VendorRule, CategorizationDecision


class AccountRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get_id_by_code(self, account_code: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM accounts WHERE account_code=?", (account_code,)
        ).fetchone()
        if not row:
            raise ValueError(f"Unknown account_code: {account_code}")
        return row["id"]

    def list_active(self) -> List[sqlite3.Row]:
        return self.conn.execute("""
            SELECT a.*, o.name AS owner_name
            FROM accounts a
            LEFT JOIN owners o ON a.owner_id = o.id
            WHERE a.is_active = 1
            ORDER BY a.account_type, a.display_name
        """).fetchall()

    def list_all_with_owner(self) -> List[sqlite3.Row]:
        return self.conn.execute("""
            SELECT a.*, o.name AS owner_name
            FROM accounts a
            LEFT JOIN owners o ON a.owner_id = o.id
            ORDER BY a.is_active DESC, a.account_type, a.display_name
        """).fetchall()

    def list_owners(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT id, name FROM owners ORDER BY name").fetchall()

    def add_owner(self, name: str) -> int:
        cur = self.conn.execute("INSERT INTO owners (name) VALUES (?)", (name,))
        return cur.lastrowid

    def add(self, account_code, display_name, description, account_type,
            institution, last4, currency, owner_id, statement_format=None) -> int:
        cur = self.conn.execute("""
            INSERT INTO accounts
                (account_code, display_name, description, account_type,
                 institution, account_number_last4, currency, owner_id, statement_format)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (account_code, display_name, description or None, account_type,
              institution or None, last4 or None, currency, owner_id, statement_format or None))
        return cur.lastrowid

    def deactivate(self, account_id: int):
        self.conn.execute(
            "UPDATE accounts SET is_active=0 WHERE id=?", (account_id,)
        )

    def latest_snapshot(self, account_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("""
            SELECT s.* FROM account_snapshots s
            WHERE s.account_id = ? AND s.is_hidden = 0
            ORDER BY s.year_month DESC LIMIT 1
        """, (account_id,)).fetchone()

    def list_by_statement_format(self, statement_format: str) -> List[sqlite3.Row]:
        """
        All active accounts wired to a given PDF parser. Used by the import
        screen to show a target-account picker whenever more than one
        account shares a format (e.g. two people's Chase Checking accounts)
        instead of silently importing into a single hardcoded account.
        """
        return self.conn.execute("""
            SELECT id, account_code, display_name FROM accounts
            WHERE statement_format = ? AND is_active = 1
            ORDER BY display_name
        """, (statement_format,)).fetchall()

    def list_all_importable(self) -> List[sqlite3.Row]:
        """
        All active accounts for the import screen's account picker. PDF-parser
        accounts are grouped by format; null-format accounts appear under the
        OFX / manual group since OFX files can target any account.
        """
        return self.conn.execute("""
            SELECT id, account_code, display_name, statement_format FROM accounts
            WHERE is_active = 1
            ORDER BY CASE WHEN statement_format IS NULL THEN 1 ELSE 0 END,
                     statement_format, display_name
        """).fetchall()


class CategoryRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get_id_by_name(self, name: str) -> Optional[int]:
        row = self.conn.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
        return row["id"] if row else None

    def list_all(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM categories ORDER BY name").fetchall()

    def list_all_with_counts(self) -> List[sqlite3.Row]:
        return self.conn.execute("""
            SELECT c.*, p.name as parent_name,
                   COUNT(t.id) as txn_count,
                   COUNT(vr.id) as rule_count
            FROM categories c
            LEFT JOIN categories p ON c.parent_id = p.id
            LEFT JOIN transactions t ON t.category_id = c.id
            LEFT JOIN vendor_rules vr ON vr.category_id = c.id AND vr.is_active = 1
            GROUP BY c.id
            ORDER BY c.name
        """).fetchall()

    def add(self, name: str, parent_id: Optional[int], money_type: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO categories (name, parent_id, money_type) VALUES (?, ?, ?)",
            (name, parent_id or None, money_type)
        )
        return cur.lastrowid

    def delete(self, category_id: int) -> str:
        """Returns 'deleted' or 'in_use' (has transactions referencing it)."""
        txn_count = self.conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE category_id=?", (category_id,)
        ).fetchone()[0]
        if txn_count:
            return "in_use"
        self.conn.execute("DELETE FROM vendor_rules WHERE category_id=?", (category_id,))
        self.conn.execute("DELETE FROM categories WHERE id=?", (category_id,))
        return "deleted"


class VendorRuleRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add_rule(self, pattern: str, match_type: str, category_id: Optional[int],
                  money_type: str, confidence: str, is_pending: bool = False) -> int:
        cur = self.conn.execute("""
            INSERT INTO vendor_rules (pattern, match_type, category_id, money_type, confidence, is_pending)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (pattern, match_type, category_id, money_type, confidence, 1 if is_pending else 0))
        return cur.lastrowid

    def deactivate(self, rule_id: int) -> None:
        self.conn.execute("UPDATE vendor_rules SET is_active=0 WHERE id=?", (rule_id,))

    def approve(self, rule_id: int) -> None:
        self.conn.execute("UPDATE vendor_rules SET is_pending=0 WHERE id=?", (rule_id,))

    def find_active_exact(self, pattern: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("""
            SELECT id, category_id, money_type FROM vendor_rules
            WHERE pattern = ? AND match_type = 'exact' AND is_active = 1
        """, (pattern,)).fetchone()

    def upsert_exact(self, pattern: str, category_id: int, money_type: str) -> None:
        """
        Suggest or correct a high-confidence exact-match rule for a full
        description string, once the user's manual categorizations for that
        exact description are unanimous — see app._maybe_promote_vendor_rule().

        New suggestions and corrections to existing rules are created/flagged
        with is_pending=1: categorize() (via list_active()) ignores pending
        rules, so nothing is auto-applied until the user approves it on the
        Vendor Rules page.
        """
        existing = self.find_active_exact(pattern)
        if existing:
            if existing["category_id"] != category_id or existing["money_type"] != money_type:
                self.conn.execute("""
                    UPDATE vendor_rules SET category_id=?, money_type=?, confidence='high', is_pending=1
                    WHERE id=?
                """, (category_id, money_type, existing["id"]))
        else:
            self.add_rule(pattern, "exact", category_id, money_type, "high", is_pending=True)

    def list_active(self) -> List[VendorRule]:
        """
        Loads all APPROVED rules ONCE as plain VendorRule objects, for the
        caller to pass into domain.categorization.categorize() — this is
        what eliminates the original per-transaction database round-trip.
        Pending (unapproved) suggestions are excluded so they never affect
        categorization until a human approves them.
        """
        rows = self.conn.execute("""
            SELECT id, pattern, match_type, category_id, money_type, owner_id, confidence
            FROM vendor_rules WHERE is_active = 1 AND is_pending = 0
        """).fetchall()
        return [
            VendorRule(
                id=r["id"], pattern=r["pattern"], match_type=r["match_type"],
                category_id=r["category_id"], money_type=r["money_type"],
                owner_id=r["owner_id"], confidence=r["confidence"], is_active=True,
            )
            for r in rows
        ]

    def list_all_with_names(self) -> List[sqlite3.Row]:
        return self.conn.execute("""
            SELECT vr.*, c.name as category_name, o.name as owner_name
            FROM vendor_rules vr
            LEFT JOIN categories c ON vr.category_id = c.id
            LEFT JOIN owners o ON vr.owner_id = o.id
            WHERE vr.is_active = 1
            ORDER BY vr.is_pending DESC, LENGTH(vr.pattern) DESC
        """).fetchall()

    def update_rule(self, rule_id: int, pattern: str, match_type: str,
                    category_id, money_type: str, confidence: str) -> None:
        self.conn.execute("""
            UPDATE vendor_rules
            SET pattern=?, match_type=?, category_id=?, money_type=?, confidence=?,
                updated_at=datetime('now')
            WHERE id=?
        """, (pattern, match_type, category_id or None, money_type, confidence, rule_id))


class ExchangeRateRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get_rate(self, year_month: str, currency: str) -> float:
        if currency == "GBP":  # reporting currency itself
            return 1.0
        row = self.conn.execute(
            "SELECT rate_to_reporting FROM exchange_rates WHERE year_month=? AND currency=?",
            (year_month, currency),
        ).fetchone()
        if not row:
            raise ValueError(f"No exchange rate found for {currency} in {year_month}")
        return row["rate_to_reporting"]


class TransactionRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def already_imported(self, account_id: int, source_statement: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM transactions WHERE account_id=? AND source_statement=? LIMIT 1",
            (account_id, source_statement),
        ).fetchone()
        return row is not None

    def existing_fingerprint_counts(self, account_id: int) -> "Counter":
        """
        Snapshot of (date, description, rounded settlement_amount) -> count
        for every transaction already persisted for this account, taken ONCE
        before a batch import starts.

        Used instead of a live per-row duplicate lookup: two genuinely
        distinct transactions on the same day for the same amount and vendor
        (e.g. two identical transit top-ups, two identical subscription
        charges) are common and legitimate. A live lookup would insert the
        first, then immediately see it as a "duplicate" match for the
        second — silently dropping a real transaction. Snapshotting the
        count beforehand and decrementing it as matches are consumed means
        only fingerprints that existed BEFORE this batch can ever be flagged,
        so within-batch repeats never shadow each other.
        """
        from collections import Counter
        rows = self.conn.execute("""
            SELECT transaction_date, description, ROUND(settlement_amount, 2) as amt
            FROM transactions WHERE account_id=?
        """, (account_id,)).fetchall()
        return Counter((r["transaction_date"], r["description"], r["amt"]) for r in rows)

    def category_consistency_for_description(self, description: str) -> List[sqlite3.Row]:
        """
        Distinct (category_id, money_type) combinations across all categorized
        transactions sharing this exact description. Used to decide whether a
        vendor rule can be safely auto-promoted — see app._maybe_promote_vendor_rule().
        """
        return self.conn.execute("""
            SELECT DISTINCT category_id, money_type
            FROM transactions
            WHERE description = ? AND category_id IS NOT NULL
        """, (description,)).fetchall()

    def count_by_description(self, description: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE description = ?", (description,)
        ).fetchone()[0]

    def insert(self, account_id: int, txn: RawTransaction, decision: CategorizationDecision,
               reporting_amount: float, source_statement: str) -> int:
        cur = self.conn.execute("""
            INSERT INTO transactions
                (account_id, transaction_date, description, raw_description,
                 transaction_currency, transaction_amount, settlement_currency, settlement_amount,
                 fx_rate, reporting_amount, category_id, money_type, owner_id,
                 balance_after, confidence, matched_rule_id, source_statement)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            account_id, txn.date, txn.description, " | ".join(txn.raw_source_lines),
            txn.transaction_currency, txn.transaction_amount,
            txn.settlement_currency, txn.settlement_amount,
            txn.fx_rate, reporting_amount, decision.category_id, decision.money_type,
            decision.owner_id, txn.balance_after, decision.confidence,
            decision.matched_rule_id, source_statement,
        ))
        return cur.lastrowid

    def hide_month(self, year_month: str) -> int:
        """
        Mark every transaction in a calendar month as hidden, across all
        accounts — used to exclude an early/incomplete period from every
        view and report without destroying the underlying rows (e.g.
        starting "official" tracking from a later month while keeping
        earlier data on file for reference). Returns the number of rows hidden.
        """
        cur = self.conn.execute(
            "UPDATE transactions SET is_hidden=1 WHERE substr(transaction_date,1,7)=?",
            (year_month,)
        )
        return cur.rowcount

    def available_months(self) -> List[str]:
        return [r[0] for r in self.conn.execute("""
            SELECT DISTINCT substr(transaction_date, 1, 7) as ym
            FROM transactions WHERE is_hidden = 0 ORDER BY ym DESC LIMIT 24
        """).fetchall()]

    def _filter_clause(self, account_id: Optional[int] = None, category_id: Optional[int] = None,
                        confidence: Optional[str] = None, year_month: Optional[str] = None,
                        description: Optional[str] = None) -> tuple:
        """
        Shared WHERE-clause builder for list_filtered() and
        totals_for_filtered() — kept in one place so the displayed total
        can never drift from the displayed rows (e.g. one query gaining a
        filter the other forgets).
        """
        clause = " WHERE t.is_hidden = 0"
        params = []
        if account_id:
            clause += " AND t.account_id = ?"
            params.append(account_id)
        if category_id:
            clause += " AND t.category_id = ?"
            params.append(category_id)
        if confidence:
            clause += " AND t.confidence = ?"
            params.append(confidence)
        if year_month:
            clause += " AND substr(t.transaction_date, 1, 7) = ?"
            params.append(year_month)
        if description:
            clause += " AND t.description LIKE ?"
            params.append(f"%{description}%")
        return clause, params

    def list_filtered(self, account_id: Optional[int] = None, category_id: Optional[int] = None,
                       confidence: Optional[str] = None, year_month: Optional[str] = None,
                       description: Optional[str] = None, limit: int = 500) -> List[sqlite3.Row]:
        clause, params = self._filter_clause(account_id, category_id, confidence, year_month, description)
        query = f"""
            SELECT t.*, a.display_name as account_name, c.name as category_name,
                   o.name as owner_name, tr.name as trip_name
            FROM transactions t
            LEFT JOIN accounts a ON t.account_id = a.id
            LEFT JOIN categories c ON t.category_id = c.id
            LEFT JOIN owners o ON t.owner_id = o.id
            LEFT JOIN trips tr ON t.trip_id = tr.id
            {clause}
            ORDER BY t.transaction_date DESC LIMIT ?
        """
        params = params + [limit]
        return self.conn.execute(query, params).fetchall()

    def totals_for_filtered(self, account_id: Optional[int] = None, category_id: Optional[int] = None,
                             confidence: Optional[str] = None, year_month: Optional[str] = None,
                             description: Optional[str] = None) -> dict:
        """
        Income/expense/net totals (in reporting_amount, the one field safe
        to sum across accounts with different native currencies) for
        exactly the same rows list_filtered() would return with the same
        filters — NOT capped by list_filtered()'s row limit, so the total
        is correct even when more rows match than are displayed.

        Transfers are excluded from the net (moving money between your own
        accounts isn't income or spend). Reimbursements count as a
        reduction against expense, same convention as the category chart.
        """
        clause, params = self._filter_clause(account_id, category_id, confidence, year_month, description)
        row = self.conn.execute(f"""
            SELECT
                SUM(CASE WHEN money_type='income' THEN reporting_amount ELSE 0 END) as income,
                SUM(CASE WHEN money_type='expense' THEN -reporting_amount ELSE 0 END) as expense,
                SUM(CASE WHEN money_type='reimbursement' THEN reporting_amount ELSE 0 END) as reimbursement,
                COUNT(*) as n
            FROM transactions t
            {clause}
        """, params).fetchone()
        income = row["income"] or 0.0
        expense = row["expense"] or 0.0
        reimbursement = row["reimbursement"] or 0.0
        return {
            "income": income,
            "expense": expense,
            "reimbursement": reimbursement,
            "net": income - expense + reimbursement,
            "count": row["n"] or 0,
        }

    def monthly_spend_by_category(self, year_months: List[str]) -> List[sqlite3.Row]:
        """
        (year_month, category_id, category_name, spend) for every category
        with any expense activity in the given months — backs the Analysis
        page's stacked bar chart. Expense-only, same sign convention as
        spend_by_category_for_month() (reporting_amount negated to a
        positive spend figure); reimbursements are NOT netted against
        expense here since they're a separate money_type and shown/selected
        as their own category series if the user wants them.
        """
        if not year_months:
            return []
        placeholders = ",".join("?" * len(year_months))
        return self.conn.execute(f"""
            SELECT substr(t.transaction_date, 1, 7) as year_month,
                   c.id as category_id, c.name as category_name,
                   SUM(-t.reporting_amount) as spend
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.money_type = 'expense' AND t.is_hidden = 0
              AND substr(t.transaction_date, 1, 7) IN ({placeholders})
            GROUP BY year_month, c.id
            ORDER BY year_month, spend DESC
        """, year_months).fetchall()

    def update_categorization(self, txn_id: int, category_id: Optional[int], money_type: Optional[str],
                               trip_id: Optional[int]) -> Optional[int]:
        """
        Returns the previous category_id, for audit logging by the caller.

        category_id and money_type use COALESCE: pass None for either to
        leave that field untouched (e.g. a bulk "set category only" action
        that shouldn't clobber each row's own money type). trip_id is set
        unconditionally — callers that want to preserve the existing trip
        must pass its current value explicitly, since None here means
        "clear the trip assignment" (used by the single-transaction edit form).
        """
        old = self.conn.execute("SELECT category_id FROM transactions WHERE id=?", (txn_id,)).fetchone()
        self.conn.execute("""
            UPDATE transactions
            SET category_id = COALESCE(?, category_id),
                money_type  = COALESCE(?, money_type),
                trip_id=?, confidence='high', updated_at=datetime('now')
            WHERE id=?
        """, (category_id, money_type, trip_id, txn_id))
        return old["category_id"] if old else None

    def get_full(self, txn_id: int) -> Optional[sqlite3.Row]:
        """Full detail for the shared edit modal — every read-only field
        shown for context plus the three editable ones (category, money
        type, trip)."""
        return self.conn.execute("""
            SELECT t.*, a.display_name as account_name, c.name as category_name,
                   o.name as owner_name, tr.name as trip_name
            FROM transactions t
            LEFT JOIN accounts a ON t.account_id = a.id
            LEFT JOIN categories c ON t.category_id = c.id
            LEFT JOIN owners o ON t.owner_id = o.id
            LEFT JOIN trips tr ON t.trip_id = tr.id
            WHERE t.id = ?
        """, (txn_id,)).fetchone()

    def set_full_categorization(self, txn_id: int, category_id: Optional[int],
                                 money_type: str, trip_id: Optional[int]) -> Optional[int]:
        """
        Direct (non-COALESCE) update for the shared single-transaction edit
        modal: unlike update_categorization() — built for bulk actions where
        an omitted field means "leave it alone" — a full-transaction edit
        always submits its complete intended state, including explicitly
        clearing category or trip to none. Returns the previous category_id
        for audit logging.
        """
        old = self.conn.execute("SELECT category_id FROM transactions WHERE id=?", (txn_id,)).fetchone()
        self.conn.execute("""
            UPDATE transactions
            SET category_id=?, money_type=?, trip_id=?, confidence='high', updated_at=datetime('now')
            WHERE id=?
        """, (category_id, money_type, trip_id, txn_id))
        return old["category_id"] if old else None

    def monthly_income_expense_totals(self, limit_months: int = 6) -> List[sqlite3.Row]:
        return self.conn.execute("""
            SELECT substr(transaction_date, 1, 7) as year_month,
                   SUM(CASE WHEN money_type='income' THEN reporting_amount ELSE 0 END) as income,
                   SUM(CASE WHEN money_type='expense' THEN reporting_amount ELSE 0 END) as expense
            FROM transactions
            WHERE is_hidden = 0
            GROUP BY year_month
            ORDER BY year_month DESC
            LIMIT ?
        """, (limit_months,)).fetchall()

    def spend_by_category_for_month(self, year_month: str) -> List[sqlite3.Row]:
        return self.conn.execute("""
            SELECT c.name as category_name,
                   SUM(-t.reporting_amount) as spend
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.money_type = 'expense'
              AND t.is_hidden = 0
              AND substr(t.transaction_date, 1, 7) = ?
            GROUP BY c.id
            ORDER BY spend DESC
            LIMIT 14
        """, (year_month,)).fetchall()

    def count_all(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM transactions WHERE is_hidden = 0").fetchone()[0]


class TripRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def list_with_totals(self) -> List[sqlite3.Row]:
        return self.conn.execute("""
            SELECT tr.*,
                   COUNT(t.id) as txn_count,
                   SUM(CASE WHEN t.money_type='expense' THEN -t.reporting_amount ELSE 0 END) as total_spend
            FROM trips tr
            LEFT JOIN transactions t ON t.trip_id = tr.id AND t.is_hidden = 0
            GROUP BY tr.id
            ORDER BY tr.start_date DESC
        """).fetchall()

    def get(self, trip_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()

    def spend_by_account(self, trip_id: int) -> List[sqlite3.Row]:
        return self.conn.execute("""
            SELECT a.display_name as account_name, a.currency,
                   SUM(CASE WHEN t.money_type='expense' THEN -t.settlement_amount ELSE 0 END) as native_spend,
                   SUM(CASE WHEN t.money_type='expense' THEN -t.reporting_amount ELSE 0 END) as reporting_spend
            FROM transactions t JOIN accounts a ON t.account_id = a.id
            WHERE t.trip_id = ? AND t.is_hidden = 0
            GROUP BY a.id
        """, (trip_id,)).fetchall()

    def spend_by_category(self, trip_id: int) -> List[sqlite3.Row]:
        return self.conn.execute("""
            SELECT c.name as category_name,
                   SUM(CASE WHEN t.money_type='expense' THEN -t.reporting_amount ELSE 0 END) as reporting_spend
            FROM transactions t JOIN categories c ON t.category_id = c.id
            WHERE t.trip_id = ? AND t.is_hidden = 0
            GROUP BY c.id
            ORDER BY reporting_spend DESC
        """, (trip_id,)).fetchall()

    def add(self, name: str, start_date: Optional[str], end_date: Optional[str], notes: Optional[str]) -> int:
        cur = self.conn.execute(
            "INSERT INTO trips (name, start_date, end_date, notes) VALUES (?, ?, ?, ?)",
            (name, start_date or None, end_date or None, notes or None)
        )
        return cur.lastrowid

    def delete(self, trip_id: int) -> None:
        self.conn.execute("UPDATE transactions SET trip_id=NULL WHERE trip_id=?", (trip_id,))
        self.conn.execute("DELETE FROM trips WHERE id=?", (trip_id,))

    def transactions_for_trip(self, trip_id: int) -> List[sqlite3.Row]:
        return self.conn.execute("""
            SELECT t.*, a.display_name as account_name, c.name as category_name
            FROM transactions t
            LEFT JOIN accounts a ON t.account_id = a.id
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE t.trip_id = ? AND t.is_hidden = 0
            ORDER BY t.transaction_date
        """, (trip_id,)).fetchall()


class ReviewQueueRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, transaction_id: int, issue_description: str, severity: str) -> None:
        self.conn.execute("""
            INSERT INTO review_queue (transaction_id, issue_description, severity)
            VALUES (?, ?, ?)
        """, (transaction_id, issue_description, severity))

    def list_open(self) -> List[sqlite3.Row]:
        return self.conn.execute("""
            SELECT r.*, t.transaction_date, t.description,
                   t.transaction_amount, t.transaction_currency,
                   t.settlement_amount, t.settlement_currency,
                   t.money_type, t.category_id,
                   a.display_name as account_name, a.account_code,
                   c.name as category_name
            FROM review_queue r
            JOIN transactions t ON r.transaction_id = t.id
            JOIN accounts a ON t.account_id = a.id
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE r.status = 'open' AND t.is_hidden = 0
            ORDER BY r.severity DESC, t.transaction_date
        """).fetchall()

    def count_open(self) -> int:
        return self.conn.execute("""
            SELECT COUNT(*) FROM review_queue r
            JOIN transactions t ON r.transaction_id = t.id
            WHERE r.status='open' AND t.is_hidden = 0
        """).fetchone()[0]

    def count_by_severity(self) -> dict:
        rows = self.conn.execute("""
            SELECT r.severity, COUNT(*) as n FROM review_queue r
            JOIN transactions t ON r.transaction_id = t.id
            WHERE r.status='open' AND t.is_hidden = 0
            GROUP BY r.severity
        """).fetchall()
        return {r["severity"]: r["n"] for r in rows}

    def resolve_for_transaction(self, transaction_id: int) -> None:
        self.conn.execute(
            "UPDATE review_queue SET status='resolved', resolved_at=datetime('now') WHERE transaction_id=?",
            (transaction_id,),
        )

    def list_open_matching(self, pattern: str, match_type: str) -> List[sqlite3.Row]:
        base_select = """
            SELECT r.transaction_id, t.description, t.transaction_date,
                   t.transaction_amount, t.transaction_currency, a.display_name as account_name
            FROM review_queue r
            JOIN transactions t ON r.transaction_id = t.id
            JOIN accounts a ON t.account_id = a.id
            WHERE r.status = 'open' AND t.is_hidden = 0
        """
        if match_type == "contains":
            return self.conn.execute(
                base_select + " AND UPPER(t.description) LIKE '%' || UPPER(?) || '%' ORDER BY t.transaction_date DESC LIMIT 100",
                (pattern,),
            ).fetchall()
        if match_type == "exact":
            return self.conn.execute(
                base_select + " AND UPPER(t.description) = UPPER(?) ORDER BY t.transaction_date DESC LIMIT 100",
                (pattern,),
            ).fetchall()
        # regex — filter in Python
        import re
        all_rows = self.conn.execute(base_select + " ORDER BY t.transaction_date DESC").fetchall()
        try:
            return [r for r in all_rows if re.search(pattern, r["description"], re.IGNORECASE)][:100]
        except re.error:
            return []


class AuditLogRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def record(self, entity_type: str, entity_id: int, field_name: str,
               old_value, new_value, reason: str) -> None:
        self.conn.execute("""
            INSERT INTO audit_log (entity_type, entity_id, field_name, old_value, new_value, reason)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (entity_type, entity_id, field_name, str(old_value), str(new_value), reason))


class SnapshotRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def coverage(self, account_ids: List[int], year_months: List[str]) -> dict:
        """Returns {(account_id, year_month): 'loaded'|'partial'} for existing snapshots."""
        if not account_ids or not year_months:
            return {}
        ph_ids = ",".join("?" * len(account_ids))
        ph_yms = ",".join("?" * len(year_months))
        rows = self.conn.execute(f"""
            SELECT account_id, year_month, reconciled
            FROM account_snapshots
            WHERE account_id IN ({ph_ids}) AND year_month IN ({ph_yms}) AND is_hidden = 0
        """, account_ids + year_months).fetchall()
        return {(r["account_id"], r["year_month"]): ("loaded" if r["reconciled"] else "partial")
                for r in rows}

    def upsert(self, account_id: int, year_month: str, closing_balance_native: float,
               statement_closing_balance: float, reconciled: bool, reconciliation_diff: float) -> None:
        self.conn.execute("""
            INSERT OR REPLACE INTO account_snapshots
                (account_id, year_month, closing_balance_native, statement_closing_balance,
                 reconciled, reconciliation_diff)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (account_id, year_month, closing_balance_native, statement_closing_balance,
              1 if reconciled else 0, reconciliation_diff))

    def hide_month(self, year_month: str) -> int:
        """Companion to TransactionRepository.hide_month() — hides the matching
        snapshot too, so a hidden month shows as 'awaited' on the coverage
        chart and drops out of net-worth calculations, instead of still
        counting as a loaded/reconciled period."""
        cur = self.conn.execute(
            "UPDATE account_snapshots SET is_hidden=1 WHERE year_month=?", (year_month,)
        )
        return cur.rowcount
