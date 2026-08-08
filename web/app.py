"""
Flask web layer.  Zero SQL strings live here — every query goes through
persistence/repositories.py.  Routes do three things only:
  1. open a connection (via contextlib.closing so it closes on any exit path)
  2. call repositories / compute presentation-layer values
  3. render a template or return JSON
"""
import logging
import os
import sys
import uuid
from contextlib import closing
from collections import defaultdict
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from flask import Flask, render_template, request, redirect, url_for, jsonify
from werkzeug.utils import secure_filename
from web.import_service import TEMP_DIR, detect_pdf, start_job, get_jobs

from persistence.database import get_connection
from persistence.migrations import pending_migrations
from persistence.repositories import (
    AccountRepository, TransactionRepository, CategoryRepository, TripRepository,
    ReviewQueueRepository, VendorRuleRepository, AuditLogRepository, SnapshotRepository,
    ExchangeRateRepository,
)

app = Flask(__name__)

# All deployment-sensitive settings come from the environment — see
# .env.example. SECRET_KEY gets a random per-process fallback so a dev
# instance works out of the box, but anything session-dependent would
# reset on restart; production must set it explicitly.
app.config["SECRET_KEY"] = os.environ.get("PULSE_SECRET_KEY") or os.urandom(32).hex()
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("PULSE_MAX_UPLOAD_MB", "16")) * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

from web.csrf import init_csrf
init_csrf(app)


logging.basicConfig(
    level=os.environ.get("PULSE_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("pulse")


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


def _wants_json() -> bool:
    return request.path.startswith("/api/")


@app.errorhandler(404)
def _not_found(e):
    if _wants_json():
        return jsonify({"ok": False, "error": "Not found"}), 404
    return "Not found", 404


@app.errorhandler(413)
def _too_large(e):
    limit = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return jsonify({"ok": False, "error": f"File too large (limit {limit} MB)"}), 413


@app.errorhandler(500)
def _server_error(e):
    log.exception("Unhandled error on %s %s", request.method, request.path)
    if _wants_json():
        return jsonify({"ok": False, "error": "Internal server error"}), 500
    return "Something went wrong — check the server log for details.", 500


_schema_checked = False

@app.before_request
def _ensure_db_initialized():
    """Fail with a clear instruction instead of a cryptic 'no such table'
    traceback when the app is started before cli/init_db.py has ever run."""
    global _schema_checked
    if _schema_checked:
        return
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
        ).fetchone()
    if not row:
        return ("Database not initialized. Run:  python3 cli/init_db.py "
                "(optionally PULSE_DB_PATH=<path> to choose a location)"), 503

    with closing(get_connection()) as conn:
        pending = pending_migrations(conn)
    if pending:
        return ("Database schema is out of date (pending migrations: "
                 f"{', '.join(pending)}). Run:  python3 cli/migrate.py"), 503

    _schema_checked = True


def _int_arg(name: str, default: int = 0, minimum: int = None) -> int:
    """request.args integer with garbage tolerance: '?offset=abc' should
    degrade to the default, not 500 the page."""
    try:
        val = int(request.args.get(name, default) or default)
    except (TypeError, ValueError):
        val = default
    if minimum is not None:
        val = max(minimum, val)
    return val

# ── helpers ──────────────────────────────────────────────────────────────────

CCY_SYMBOL = {"GBP": "£", "USD": "$", "EUR": "€", "INR": "₹",
               "CAD": "CA$", "AUD": "A$", "CHF": "Fr"}
MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
ACCOUNT_COLORS = {
    "HSBC_UK":         "#5fae77",
    "CHASE_CHECKING":  "#a78bfa",
    "CHASE_SAVINGS":   "#9bb9ef",
    "CHASE_SAPPHIRE":  "#6a9bf0",
}
# Deterministic per-category color for the Analysis stacked bar chart —
# category_id % len(palette) so a given category always renders the same
# color across page loads/offsets without needing a color column in the DB.
CATEGORY_PALETTE = [
    "#6a9bf0", "#5fae77", "#d6a23f", "#d2694f", "#a78bfa", "#e879b9",
    "#4fd1c5", "#f0b429", "#8b9dc3", "#c9a876", "#7fb3d5", "#e8836a",
    "#9dd67f", "#b088c9",
]


def _recent_months(n: int = 4, offset: int = 0) -> list:
    """Return n months as 'YYYY-MM' strings (oldest first), shifted back by offset months."""
    today = date.today()
    year, month = today.year, today.month
    for _ in range(offset):
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    result = []
    for _ in range(n):
        result.insert(0, f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return result


def _build_chart_data(monthly_totals) -> list:
    months_asc = list(reversed(list(monthly_totals)))
    incomes  = [abs(m["income"]  or 0) for m in months_asc]
    expenses = [abs(m["expense"] or 0) for m in months_asc]
    max_val  = max(max(incomes, default=0), max(expenses, default=0), 1)
    max_exp_idx = expenses.index(max(expenses)) if expenses else -1
    max_inc_idx = incomes.index(max(incomes))   if incomes  else -1
    result = []
    for i, m in enumerate(months_asc):
        mm = int(m["year_month"][5:7])
        label = MONTH_ABBR[mm]
        if i == max_exp_idx and (not incomes or expenses[i] > incomes[i]):
            label_color = "var(--c-amber)"
            label += " ↑"
        elif i == max_inc_idx and (not expenses or incomes[i] >= expenses[i]):
            label_color = "var(--c-green-t)"
            label += " ↑"
        else:
            label_color = "var(--c-faint)"
        result.append({
            "ym":          m["year_month"],
            "label":       label,
            "label_color": label_color,
            "income_h":    round(incomes[i]  / max_val * 156),
            "expense_h":   round(expenses[i] / max_val * 156),
        })
    return result


@app.context_processor
def _inject_nav_globals():
    """Inject review count into every template for the nav badge."""
    with closing(get_connection()) as conn:
        review_count = ReviewQueueRepository(conn).count_open()
    return {"nav_review_count": review_count}


def _maybe_promote_vendor_rule(conn, description: str, category_id, money_type: str) -> None:
    """
    After a manual categorization, check whether every past transaction with
    this exact description agrees on the same category + money_type. If so
    (and there are at least 2 data points), auto-create/update a high-confidence
    exact-match vendor rule so future imports of the same description skip
    the review queue entirely instead of needing to be resolved again.
    """
    if not category_id or not description:
        return
    rows = TransactionRepository(conn).category_consistency_for_description(description)
    if len(rows) != 1:
        return  # disagreement across history — don't guess, leave for manual review
    only = rows[0]
    if only["category_id"] != int(category_id) or only["money_type"] != money_type:
        return  # the transaction just saved hasn't committed to this txn_repo view yet
    count = TransactionRepository(conn).count_by_description(description)
    if count < 2:
        return  # single occurrence isn't enough evidence to auto-apply going forward
    VendorRuleRepository(conn).upsert_exact(description, int(category_id), money_type)


@app.template_filter("ccy")
def _ccy(currency):
    return CCY_SYMBOL.get(currency, currency + " ")


@app.template_filter("fmt")
def _fmt(amount):
    if amount is None:
        return "—"
    return f"{abs(amount):,.2f}"


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    cat_month  = request.args.get("month", "")
    cov_offset = _int_arg("cov_offset", 0, minimum=0)
    with closing(get_connection()) as conn:
        account_repo  = AccountRepository(conn)
        txn_repo      = TransactionRepository(conn)
        review_repo   = ReviewQueueRepository(conn)
        snap_repo     = SnapshotRepository(conn)

        raw_accounts = account_repo.list_active()
        recent_months = _recent_months(5, offset=cov_offset)

        # Statement coverage
        acct_ids = [a["id"] for a in raw_accounts]
        coverage = snap_repo.coverage(acct_ids, recent_months)

        # Enrich accounts with snapshot + GBP equivalent
        accounts = []
        for acct in raw_accounts:
            snap = account_repo.latest_snapshot(acct["id"])
            balance = snap["closing_balance_native"] if snap else None
            is_credit = acct["account_type"] == "credit_card"
            display_balance = (-balance if is_credit else balance) if balance is not None else None

            gbp_equiv = None
            if display_balance is not None and acct["currency"] != "GBP":
                rate_row = conn.execute("""
                    SELECT rate_to_reporting FROM exchange_rates
                    WHERE currency=? ORDER BY year_month DESC LIMIT 1
                """, (acct["currency"],)).fetchone()
                rate = rate_row["rate_to_reporting"] if rate_row else 1.0
                gbp_equiv = display_balance * rate

            acct_months = [
                {"ym": ym,
                 "abbr": MONTH_ABBR[int(ym[5:7])] + " '" + ym[2:4],
                 "status": coverage.get((acct["id"], ym), "awaited")}
                for ym in recent_months
            ]
            accounts.append({
                **dict(acct),
                "snap":            snap,
                "display_balance": display_balance,
                "gbp_equiv":       gbp_equiv,
                "reconciled":      bool(snap["reconciled"]) if snap else None,
                "recon_diff":      snap["reconciliation_diff"] if snap else None,
                "year_month":      snap["year_month"] if snap else None,
                "color":           ACCOUNT_COLORS.get(acct["account_code"], "#6a9bf0"),
                "months":          acct_months,
            })

        review_count     = review_repo.count_open()
        severity_counts  = review_repo.count_by_severity()
        txn_count        = txn_repo.count_all()
        monthly_totals   = txn_repo.monthly_income_expense_totals(limit_months=6)
        chart_data       = _build_chart_data(monthly_totals)
        # Net worth from the per-account balances computed above — avoids
        # _net_worth_gbp's re-run of the same snapshot + FX queries per account.
        net_worth = sum(
            (a["display_balance"] if a["currency"] == "GBP" else (a["gbp_equiv"] or 0.0))
            for a in accounts if a["display_balance"] is not None
        )
        avail_months     = txn_repo.available_months()
        selected_month   = cat_month if cat_month in avail_months else (avail_months[0] if avail_months else "")
        cat_spend_raw    = txn_repo.spend_by_category_for_month(selected_month) if selected_month else []

    # Build category spend bars
    max_cat = max((r["spend"] or 0 for r in cat_spend_raw), default=1)
    cat_spend = [
        {**dict(r), "bar_w": round((r["spend"] or 0) / max_cat * 100)}
        for r in cat_spend_raw
    ]

    return render_template(
        "dashboard.html",
        accounts=accounts,
        review_count=review_count,
        review_low=severity_counts.get("low", 0),
        review_med=severity_counts.get("medium", 0),
        txn_count=txn_count,
        chart_data=chart_data,
        net_worth=net_worth,
        recent_months=recent_months,
        avail_months=avail_months,
        selected_month=selected_month,
        cat_spend=cat_spend,
        cov_offset=cov_offset,
    )


@app.route("/transactions")
def transactions():
    account_filter    = request.args.get("account_id",  "")
    category_filter   = request.args.get("category_id", "")
    confidence_filter = request.args.get("confidence",  "")
    month_filter      = request.args.get("year_month",  "")
    desc_filter       = request.args.get("description", "")

    PAGE_SIZE_OPTIONS = [50, 100, 200, 500]
    page      = _int_arg("page", 1, minimum=1)
    page_size = _int_arg("page_size", 100, minimum=1)
    if page_size not in PAGE_SIZE_OPTIONS:
        page_size = 100
    offset    = (page - 1) * page_size

    with closing(get_connection()) as conn:
        txn_repo = TransactionRepository(conn)
        filter_kwargs = dict(
            account_id=int(account_filter)  if account_filter.isdigit()  else None,
            category_id=int(category_filter) if category_filter.isdigit() else None,
            confidence=confidence_filter or None,
            year_month=month_filter or None,
            description=desc_filter or None,
        )
        total_count = txn_repo.count_filtered(**filter_kwargs)
        total_pages = max(1, -(-total_count // page_size))  # ceil div
        page = min(page, total_pages)
        offset = (page - 1) * page_size

        rows   = txn_repo.list_filtered(**filter_kwargs, limit=page_size, offset=offset)
        totals = txn_repo.totals_for_filtered(**filter_kwargs)
        accounts       = AccountRepository(conn).list_active()
        categories     = CategoryRepository(conn).list_all()
        trips          = TripRepository(conn).list_with_totals()
        avail_months   = txn_repo.available_months()

    return render_template(
        "transactions.html",
        transactions=rows,
        totals=totals,
        accounts=accounts,
        categories=categories,
        trips=trips,
        avail_months=avail_months,
        account_filter=account_filter,
        category_filter=category_filter,
        confidence_filter=confidence_filter,
        month_filter=month_filter,
        desc_filter=desc_filter,
        page=page,
        total_pages=total_pages,
        total_count=total_count,
        page_size=page_size,
        page_size_options=PAGE_SIZE_OPTIONS,
    )


@app.route("/analysis")
def analysis():
    offset = _int_arg("offset", 0, minimum=0)
    months = _recent_months(6, offset=offset)

    # Checkboxes share the name "categories", so the picker submits it as a
    # REPEATED query param (?categories=1&categories=2&...), not a single
    # comma-joined value — must use getlist(), plain get() would silently
    # see only the first checked box. "form_submitted" distinguishes "the
    # picker form was submitted with every box unchecked" (a real empty
    # selection) from "fresh page load, no filter applied yet" (default to
    # all) — both cases otherwise look identical (an empty categories list).
    form_submitted = request.args.get("form_submitted") == "1"
    selected_ids = {int(x) for x in request.args.getlist("categories") if x.isdigit()}

    drill_month      = request.args.get("drill_month", "")
    drill_category   = request.args.get("drill_category", "")

    with closing(get_connection()) as conn:
        txn_repo = TransactionRepository(conn)
        raw_rows = txn_repo.monthly_spend_by_category(months)

        # Categories offered in the picker: only ones with spend somewhere
        # in the visible window, so the list doesn't fill up with categories
        # that are irrelevant to the months on screen.
        cats_in_range = {}
        for r in raw_rows:
            cats_in_range.setdefault(r["category_id"], r["category_name"])
        picker_categories = sorted(cats_in_range.items(), key=lambda kv: kv[1])

        # Default selection = every category in range, the first time the
        # page loads with no explicit filter applied yet.
        active_ids = selected_ids if form_submitted else set(cats_in_range.keys())

        # Pivot into per-month stacked segments, largest spend first within
        # each month so the chart reads consistently top-to-bottom.
        by_month = defaultdict(list)
        month_totals = defaultdict(float)
        for r in raw_rows:
            if r["category_id"] not in active_ids:
                continue
            by_month[r["year_month"]].append(r)
            month_totals[r["year_month"]] += r["spend"]

        max_total = max(month_totals.values(), default=1.0) or 1.0
        chart_months = []
        for ym in months:
            segments = []
            for r in sorted(by_month.get(ym, []), key=lambda r: -r["spend"]):
                color = CATEGORY_PALETTE[r["category_id"] % len(CATEGORY_PALETTE)]
                segments.append({
                    "category_id": r["category_id"],
                    "category_name": r["category_name"],
                    "spend": r["spend"],
                    "color": color,
                    "height_pct": round(r["spend"] / max_total * 100, 2),
                })
            chart_months.append({
                "ym": ym,
                "abbr": MONTH_ABBR[int(ym[5:7])] + " '" + ym[2:4],
                "total": month_totals.get(ym, 0.0),
                "total_height_pct": round(month_totals.get(ym, 0.0) / max_total * 100, 2),
                "segments": segments,
            })

        picker = [{
            "id": cid, "name": name,
            "color": CATEGORY_PALETTE[cid % len(CATEGORY_PALETTE)],
            "selected": cid in active_ids,
        } for cid, name in picker_categories]

        drill_rows = None
        if drill_month and drill_category.isdigit():
            drill_rows = txn_repo.list_filtered(
                category_id=int(drill_category), year_month=drill_month, limit=200,
            )

    # Query-string fragment preserving the current category selection —
    # links (older/newer, bar segments) splice this in so navigating away
    # and back keeps the same filter instead of resetting to "all".
    cats_qs = "&".join(f"categories={cid}" for cid in sorted(active_ids))

    return render_template(
        "analysis.html",
        chart_months=chart_months,
        picker=picker,
        has_active_selection=bool(active_ids),
        offset=offset,
        cats_qs=cats_qs,
        drill_month=drill_month,
        drill_category=drill_category,
        drill_rows=drill_rows,
    )


@app.route("/transactions/<int:txn_id>/categorize", methods=["POST"])
def recategorize(txn_id):
    category_id = request.form.get("category_id")
    money_type  = request.form.get("money_type")
    trip_id     = request.form.get("trip_id") or None

    with closing(get_connection()) as conn:
        desc = conn.execute("SELECT description FROM transactions WHERE id=?", (txn_id,)).fetchone()["description"]
        old_cat = TransactionRepository(conn).update_categorization(
            txn_id, category_id, money_type, trip_id
        )
        AuditLogRepository(conn).record(
            "transaction", txn_id, "category_id", old_cat, category_id,
            "Manual recategorization via UI",
        )
        ReviewQueueRepository(conn).resolve_for_transaction(txn_id)
        _maybe_promote_vendor_rule(conn, desc, category_id, money_type)
        conn.commit()

    return redirect(url_for("review_queue_view"))


@app.route("/api/transactions/<int:txn_id>/set-trip", methods=["POST"])
def api_set_trip(txn_id):
    data    = request.get_json() or {}
    trip_id = data.get("trip_id") or None
    with closing(get_connection()) as conn:
        conn.execute(
            "UPDATE transactions SET trip_id=?, updated_at=datetime('now') WHERE id=?",
            (trip_id, txn_id)
        )
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/transactions/<int:txn_id>/categorize", methods=["POST"])
def api_recategorize(txn_id):
    data        = request.get_json() or {}
    category_id = data.get("category_id")
    money_type  = data.get("money_type")

    with closing(get_connection()) as conn:
        desc = conn.execute("SELECT description FROM transactions WHERE id=?", (txn_id,)).fetchone()["description"]
        old_cat = TransactionRepository(conn).update_categorization(
            txn_id, category_id, money_type, None
        )
        AuditLogRepository(conn).record(
            "transaction", txn_id, "category_id", old_cat, category_id,
            "Inline edit via transactions table",
        )
        ReviewQueueRepository(conn).resolve_for_transaction(txn_id)
        _maybe_promote_vendor_rule(conn, desc, category_id, money_type)
        conn.commit()

    return jsonify({"ok": True})


@app.route("/api/transactions/<int:txn_id>")
def api_get_transaction(txn_id):
    """Full detail for the shared edit modal (Transactions, Analysis
    drill-down, Trip detail — one endpoint backs all three)."""
    with closing(get_connection()) as conn:
        row = TransactionRepository(conn).get_full(txn_id)
        if not row:
            return jsonify({"ok": False, "error": "Transaction not found"}), 404
        return jsonify({"ok": True, "transaction": dict(row)})


@app.route("/api/transactions/<int:txn_id>/edit", methods=["POST"])
def api_edit_transaction(txn_id):
    """
    Saves the shared edit modal's three editable fields (category, money
    type, trip). Deliberately does NOT touch description or amount —
    those are facts from the source statement, not something a
    categorization layer should be able to rewrite.
    """
    data        = request.get_json() or {}
    category_id = data.get("category_id") or None
    money_type  = data.get("money_type")
    trip_id     = data.get("trip_id") or None
    if not money_type:
        return jsonify({"ok": False, "error": "money_type is required"}), 400

    with closing(get_connection()) as conn:
        txn_repo = TransactionRepository(conn)
        row = txn_repo.get_full(txn_id)
        if not row:
            return jsonify({"ok": False, "error": "Transaction not found"}), 404

        old_cat = txn_repo.set_full_categorization(txn_id, category_id, money_type, trip_id)
        AuditLogRepository(conn).record(
            "transaction", txn_id, "category_id", old_cat, category_id,
            "Edited via shared transaction modal",
        )
        ReviewQueueRepository(conn).resolve_for_transaction(txn_id)
        _maybe_promote_vendor_rule(conn, row["description"], category_id, money_type)
        conn.commit()

        updated = txn_repo.get_full(txn_id)

    return jsonify({"ok": True, "transaction": dict(updated)})


@app.route("/api/categories/list")
def api_categories_list():
    with closing(get_connection()) as conn:
        rows = CategoryRepository(conn).list_all()
    return jsonify([{"id": r["id"], "name": r["name"]} for r in rows])


@app.route("/api/trips/list")
def api_trips_list():
    with closing(get_connection()) as conn:
        rows = TripRepository(conn).list_with_totals()
    return jsonify([{"id": r["id"], "name": r["name"]} for r in rows])


@app.route("/review")
def review_queue_view():
    with closing(get_connection()) as conn:
        review_repo     = ReviewQueueRepository(conn)
        items_raw       = review_repo.list_open()
        severity_counts = review_repo.count_by_severity()
        categories      = CategoryRepository(conn).list_all()
        trips           = TripRepository(conn).list_with_totals()

    return render_template(
        "review.html",
        items=items_raw,
        categories=categories,
        trips=trips,
        review_low=severity_counts.get("low",    0),
        review_med=severity_counts.get("medium", 0),
    )


@app.route("/api/review/bulk-resolve", methods=["POST"])
def api_bulk_resolve():
    data        = request.get_json() or {}
    txn_ids     = data.get("txn_ids") or []
    category_id = data.get("category_id") or None
    # No default here: if the caller doesn't send money_type (e.g. the user
    # only wants to bulk-set a category and leave each row's own money type
    # alone), update_categorization() COALESCEs it and leaves it unchanged.
    money_type  = data.get("money_type") or None
    if not txn_ids:
        return jsonify({"ok": False, "error": "No transactions selected"}), 400
    with closing(get_connection()) as conn:
        txn_repo    = TransactionRepository(conn)
        review_repo = ReviewQueueRepository(conn)
        for txn_id in txn_ids:
            row = conn.execute("SELECT description, money_type FROM transactions WHERE id=?", (txn_id,)).fetchone()
            txn_repo.update_categorization(txn_id, category_id, money_type, None)
            review_repo.resolve_for_transaction(txn_id)
            # Promote using the row's EFFECTIVE money type (whatever it ends up
            # as) since money_type may not have been part of this bulk action.
            _maybe_promote_vendor_rule(conn, row["description"], category_id, money_type or row["money_type"])
        conn.commit()
        remaining = review_repo.count_open()
    return jsonify({"ok": True, "resolved": len(txn_ids), "remaining": remaining})


@app.route("/api/transactions/bulk-update", methods=["POST"])
def api_bulk_update_transactions():
    data        = request.get_json() or {}
    txn_ids     = data.get("txn_ids") or []
    category_id = data.get("category_id") or None
    trip_id     = data.get("trip_id") or None
    money_type  = data.get("money_type") or None
    if not txn_ids:
        return jsonify({"ok": False, "error": "No transactions selected"}), 400
    with closing(get_connection()) as conn:
        txn_repo = TransactionRepository(conn)
        for txn_id in txn_ids:
            row = conn.execute(
                "SELECT description, money_type, category_id, trip_id FROM transactions WHERE id=?", (txn_id,)
            ).fetchone()
            if not row:
                continue
            conn.execute("""
                UPDATE transactions SET
                  category_id = COALESCE(?, category_id),
                  money_type  = COALESCE(?, money_type),
                  trip_id     = CASE WHEN ? IS NOT NULL THEN ? ELSE trip_id END,
                  confidence  = 'high',
                  updated_at  = datetime('now')
                WHERE id = ?
            """, (category_id, money_type, trip_id, trip_id, txn_id))
            if category_id:
                effective_mtype = money_type or row["money_type"]
                _maybe_promote_vendor_rule(conn, row["description"], category_id, effective_mtype)
        conn.commit()
    return jsonify({"ok": True, "updated": len(txn_ids)})


@app.route("/api/review/<int:txn_id>/resolve", methods=["POST"])
def api_resolve_review(txn_id):
    data        = request.get_json() or {}
    category_id = data.get("category_id")
    money_type  = data.get("money_type", "expense")

    with closing(get_connection()) as conn:
        review_repo = ReviewQueueRepository(conn)
        txn_repo    = TransactionRepository(conn)
        desc = conn.execute("SELECT description FROM transactions WHERE id=?", (txn_id,)).fetchone()["description"]
        old_cat = txn_repo.update_categorization(txn_id, category_id, money_type, None)
        AuditLogRepository(conn).record(
            "transaction", txn_id, "category_id", old_cat, category_id,
            "Resolved via review queue",
        )
        review_repo.resolve_for_transaction(txn_id)
        _maybe_promote_vendor_rule(conn, desc, category_id, money_type)
        conn.commit()
        remaining = review_repo.count_open()

    return jsonify({"ok": True, "remaining": remaining})


@app.route("/categories")
def categories_view():
    with closing(get_connection()) as conn:
        cats       = CategoryRepository(conn).list_all_with_counts()
        top_level  = CategoryRepository(conn).list_all()
    return render_template("categories.html", categories=cats, all_categories=top_level)


@app.route("/categories/add", methods=["POST"])
def add_category():
    name       = request.form.get("name", "").strip()
    parent_id  = request.form.get("parent_id") or None
    money_type = request.form.get("money_type", "expense")
    if name:
        with closing(get_connection()) as conn:
            CategoryRepository(conn).add(name, parent_id, money_type)
            conn.commit()
    return redirect(url_for("categories_view"))


@app.route("/categories/<int:cat_id>/delete", methods=["POST"])
def delete_category(cat_id):
    with closing(get_connection()) as conn:
        result = CategoryRepository(conn).delete(cat_id)
        if result == "deleted":
            conn.commit()
            log.warning("CATEGORY-DELETE: category_id=%d", cat_id)
    return redirect(url_for("categories_view"))


@app.route("/api/trips/add", methods=["POST"])
def api_add_trip():
    data       = request.get_json() or {}
    name       = (data.get("name") or "").strip()
    start_date = data.get("start_date") or None
    end_date   = data.get("end_date") or None
    notes      = data.get("notes") or None
    if not name:
        return jsonify({"ok": False, "error": "Name required"}), 400
    with closing(get_connection()) as conn:
        new_id = TripRepository(conn).add(name, start_date, end_date, notes)
        conn.commit()
    return jsonify({"ok": True, "id": new_id})


@app.route("/api/trips/<int:trip_id>/delete", methods=["POST"])
def api_delete_trip(trip_id):
    with closing(get_connection()) as conn:
        TripRepository(conn).delete(trip_id)
        conn.commit()
        log.warning("TRIP-DELETE: trip_id=%d", trip_id)
    return jsonify({"ok": True})


@app.route("/trips")
def trips_view():
    with closing(get_connection()) as conn:
        trips = TripRepository(conn).list_with_totals()
    if trips:
        return redirect(url_for("trip_detail", trip_id=trips[0]["id"]))
    return render_template("trips.html", trips=[], trip=None,
                           by_account=[], by_category=[], transactions=[],
                           daily_bars=[], trip_id=None)


@app.route("/trips/<int:trip_id>")
def trip_detail(trip_id):
    with closing(get_connection()) as conn:
        trip_repo   = TripRepository(conn)
        trips       = trip_repo.list_with_totals()
        trip        = trip_repo.get(trip_id)
        by_account  = list(trip_repo.spend_by_account(trip_id))
        by_category = list(trip_repo.spend_by_category(trip_id))
        txns        = list(trip_repo.transactions_for_trip(trip_id))

    # Compute spend percentages for account bars
    total_acct_spend = sum(r["reporting_spend"] or 0 for r in by_account)
    max_acct_spend   = max((r["reporting_spend"] or 0 for r in by_account), default=1)
    by_account_rich = [
        {
            **dict(r),
            "pct":   round((r["reporting_spend"] or 0) / total_acct_spend * 100) if total_acct_spend else 0,
            "bar_w": round((r["reporting_spend"] or 0) / max_acct_spend   * 100) if max_acct_spend   else 0,
            "color": next(
                (ACCOUNT_COLORS[a] for a in ACCOUNT_COLORS
                 if a.split("_")[-1].lower() in r["account_name"].lower()), "#6a9bf0"
            ),
        }
        for r in by_account
    ]

    # Spend-by-category percentages
    total_cat_spend = sum(r["reporting_spend"] or 0 for r in by_category)
    max_cat_spend   = max((r["reporting_spend"] or 0 for r in by_category), default=1)
    by_category_rich = [
        {
            **dict(r),
            "pct":   round((r["reporting_spend"] or 0) / total_cat_spend * 100) if total_cat_spend else 0,
            "bar_w": round((r["reporting_spend"] or 0) / max_cat_spend   * 100) if max_cat_spend   else 0,
        }
        for r in by_category
    ]

    # Daily spend timeline
    daily: dict = defaultdict(float)
    for t in txns:
        if t["money_type"] == "expense":
            daily[t["transaction_date"]] += abs(t["reporting_amount"] or 0)
    daily_vals = [daily[d] for d in sorted(daily.keys())]
    max_daily  = max(daily_vals, default=1)
    daily_bars = [
        {"height_px": round(v / max_daily * 68), "is_peak": v == max_daily}
        for v in daily_vals
    ]

    # Trip summary stats
    days = None
    if trip and trip["start_date"] and trip["end_date"]:
        try:
            d0 = datetime.strptime(trip["start_date"], "%Y-%m-%d")
            d1 = datetime.strptime(trip["end_date"],   "%Y-%m-%d")
            days = (d1 - d0).days + 1
        except ValueError:
            pass

    total_spend  = total_acct_spend
    avg_per_day  = round(total_spend / days) if days else None

    return render_template(
        "trips.html",
        trips=trips,
        trip=trip,
        trip_id=trip_id,
        by_account=by_account_rich,
        by_category=by_category_rich,
        transactions=txns,
        daily_bars=daily_bars,
        days=days,
        total_spend=total_spend,
        avg_per_day=avg_per_day,
    )


@app.route("/vendor-rules")
def vendor_rules_view():
    with closing(get_connection()) as conn:
        rules      = VendorRuleRepository(conn).list_all_with_names()
        categories = CategoryRepository(conn).list_all()
    pending_count = sum(1 for r in rules if r["is_pending"])
    return render_template("vendor_rules.html", rules=rules, categories=categories, pending_count=pending_count)


@app.route("/api/import/detect", methods=["POST"])
def api_import_detect():
    """Save the uploaded PDF to a temp file, run detection, return results + temp_id."""
    pdf_file = request.files.get("pdf")
    if not pdf_file:
        return jsonify({"ok": False, "error": "No file provided"}), 400

    # Validate it's actually a PDF before writing to disk: the browser's
    # accept= filter is advisory only, and everything downstream
    # (pdfplumber, the parsers) assumes PDF structure. Magic bytes beat
    # trusting the filename extension or client Content-Type.
    header = pdf_file.stream.read(5)
    pdf_file.stream.seek(0)
    if header != b"%PDF-":
        return jsonify({"ok": False, "error": "Not a PDF file"}), 400

    temp_id  = str(uuid.uuid4())
    tmp_path = os.path.join(TEMP_DIR, temp_id + ".pdf")
    pdf_file.save(tmp_path)

    detection = detect_pdf(tmp_path)

    # Auto-match a specific account by (statement_format, last4) so a batch
    # of statements for the same account doesn't need re-picking per file —
    # the caller just gets one confident answer instead of a bare format.
    matched_account_id = None
    last4 = detection.pop("last4", None)
    if detection.get("account_type") and last4:
        with closing(get_connection()) as conn:
            row = conn.execute(
                "SELECT id FROM accounts WHERE statement_format=? AND account_number_last4=? AND is_active=1",
                (detection["account_type"], last4),
            ).fetchone()
            if row:
                matched_account_id = row["id"]

    return jsonify({
        "ok": True, "temp_id": temp_id, "filename": pdf_file.filename,
        "matched_account_id": matched_account_id, **detection,
    })


@app.route("/api/import/accounts-for-format/<statement_format>")
def api_accounts_for_format(statement_format):
    """
    Active accounts wired to a given PDF parser. The import screen calls
    this after detecting a statement's format to decide whether a
    target-account picker is needed (more than one account shares that
    format) or the single match can be used silently.
    """
    with closing(get_connection()) as conn:
        rows = AccountRepository(conn).list_by_statement_format(statement_format)
    return jsonify([{"id": r["id"], "code": r["account_code"], "name": r["display_name"]} for r in rows])


@app.route("/api/import/all-accounts")
def api_all_importable_accounts():
    """
    Every active account wired to any PDF parser, in one call. Backs the
    import screen's single top-level account picker — one dropdown for the
    whole card instead of picking a statement format first and then a
    target account, which was repetitive when importing several statements
    for the same account at once.
    """
    with closing(get_connection()) as conn:
        rows = AccountRepository(conn).list_all_importable()
    return jsonify([
        {"id": r["id"], "code": r["account_code"], "name": r["display_name"], "format": r["statement_format"]}
        for r in rows
    ])


@app.route("/api/import/queue", methods=["POST"])
def api_import_queue():
    """Queue a background import job using a previously uploaded temp file."""
    data        = request.get_json() or {}
    temp_id     = data.get("temp_id")
    filename    = data.get("filename", "statement.pdf")
    account_type = data.get("account_type")
    year        = data.get("year")
    start_month = data.get("start_month")
    target_account_id = data.get("target_account_id")

    if not temp_id or not account_type:
        return jsonify({"ok": False, "error": "temp_id and account_type are required"}), 400

    # temp_id is client-supplied and becomes part of a filesystem path —
    # accept only the exact UUID format this server issued in
    # /api/import/detect, otherwise "../../" traversal could address
    # arbitrary files.
    try:
        temp_id = str(uuid.UUID(temp_id))
    except (ValueError, AttributeError, TypeError):
        return jsonify({"ok": False, "error": "Invalid temp_id"}), 400

    if account_type not in ("hsbc", "chase_bank", "sapphire"):
        return jsonify({"ok": False, "error": "Unknown account_type"}), 400

    tmp_path = os.path.join(TEMP_DIR, temp_id + ".pdf")
    if not os.path.exists(tmp_path):
        return jsonify({"ok": False, "error": "Uploaded file not found — please re-upload"}), 404

    job_id = start_job(temp_id, filename, account_type, year, start_month, target_account_id)
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/import/jobs")
def api_import_jobs():
    """Return current state of all import jobs."""
    return jsonify(get_jobs())


@app.route("/accounts")
def accounts_view():
    with closing(get_connection()) as conn:
        acct_repo = AccountRepository(conn)
        accounts  = acct_repo.list_all_with_owner()
        owners    = acct_repo.list_owners()
    return render_template("accounts.html", accounts=accounts, owners=owners,
                          flash_msg=request.args.get("msg", ""))


@app.route("/accounts/add", methods=["POST"])
def add_account():
    f = request.form
    account_code = f.get("account_code", "").strip().upper().replace(" ", "_")
    display_name = f.get("display_name", "").strip()
    if not account_code or not display_name:
        return redirect(url_for("accounts_view"))
    ob_str = f.get("opening_balance_native", "").strip()
    opening_balance = float(ob_str) if ob_str else None
    with closing(get_connection()) as conn:
        acct_repo = AccountRepository(conn)
        owner_id_raw = f.get("owner_id", "1")
        if owner_id_raw == "__new__":
            new_owner_name = f.get("new_owner_name", "").strip()
            if not new_owner_name:
                return redirect(url_for("accounts_view", msg="Owner name is required"))
            existing = conn.execute(
                "SELECT id FROM owners WHERE name = ? COLLATE NOCASE", (new_owner_name,)
            ).fetchone()
            owner_id = existing["id"] if existing else acct_repo.add_owner(new_owner_name)
        else:
            try:
                owner_id = int(owner_id_raw)
            except (TypeError, ValueError):
                return redirect(url_for("accounts_view", msg="Invalid owner selection"))

        acct_id = acct_repo.add(
            account_code, display_name,
            f.get("description", "").strip(),
            f.get("account_type", "checking"),
            f.get("institution", "").strip(),
            f.get("last4", "").strip(),
            f.get("currency", "USD").strip().upper(),
            owner_id,
            statement_format=f.get("statement_format") or None,
        )
        if opening_balance is not None:
            conn.execute(
                "UPDATE accounts SET opening_balance_native=? WHERE id=?",
                (opening_balance, acct_id)
            )
        conn.commit()
    return redirect(url_for("accounts_view"))


@app.route("/owners/add", methods=["POST"])
def add_owner():
    name = request.form.get("owner_name", "").strip()
    if not name:
        return redirect(url_for("accounts_view"))
    with closing(get_connection()) as conn:
        existing = conn.execute(
            "SELECT 1 FROM owners WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if existing:
            return redirect(url_for("accounts_view", msg=f'Owner "{name}" already exists'))
        AccountRepository(conn).add_owner(name)
        conn.commit()
    return redirect(url_for("accounts_view", msg=f'Added owner "{name}"'))


@app.route("/accounts/<int:acct_id>/set-opening-balance", methods=["POST"])
def set_opening_balance(acct_id):
    ob_str = request.form.get("opening_balance_native", "").strip()
    opening_balance = float(ob_str) if ob_str else None
    with closing(get_connection()) as conn:
        conn.execute(
            "UPDATE accounts SET opening_balance_native=? WHERE id=?",
            (opening_balance, acct_id)
        )
        conn.commit()
    return redirect(url_for("accounts_view"))


@app.route("/accounts/<int:acct_id>/deactivate", methods=["POST"])
def deactivate_account(acct_id):
    with closing(get_connection()) as conn:
        AccountRepository(conn).deactivate(acct_id)
        conn.commit()
        log.warning("ACCOUNT-DEACTIVATE: account_id=%d", acct_id)
    return redirect(url_for("accounts_view"))


@app.route("/reset-data", methods=["GET"])
def reset_data_view():
    flash_msg = request.args.get("msg", "")
    with closing(get_connection()) as conn:
        txn_count   = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        rq_count    = conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
        snap_count  = conn.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0]
        audit_count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        acct_count  = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        cat_count   = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        rule_count  = conn.execute("SELECT COUNT(*) FROM vendor_rules WHERE is_active=1").fetchone()[0]
        trip_count  = conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
        avail_months = TransactionRepository(conn).available_months()
        accounts     = AccountRepository(conn).list_active()
    return render_template(
        "reset.html",
        txn_count=txn_count, rq_count=rq_count,
        snap_count=snap_count, audit_count=audit_count,
        acct_count=acct_count, cat_count=cat_count,
        rule_count=rule_count, trip_count=trip_count,
        avail_months=avail_months, accounts=accounts,
        flash_msg=flash_msg,
    )


@app.route("/reset-data", methods=["POST"])
def reset_data():
    if request.form.get("confirm") != "RESET":
        return redirect(url_for("reset_data_view"))
    with closing(get_connection()) as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("transactions", "review_queue", "account_snapshots", "audit_log")}
        TransactionRepository(conn).delete_all()  # transactions + review_queue
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM account_snapshots")
        log.warning("RESET-DATA: deleted all rows — %s", counts)
        conn.commit()
    return redirect(url_for("dashboard"))


@app.route("/reset-period", methods=["POST"])
def reset_period():
    year_month = request.form.get("year_month", "").strip()
    account_id = request.form.get("account_id") or None
    if not year_month:
        return redirect(url_for("reset_data_view"))

    with closing(get_connection()) as conn:
        deleted = TransactionRepository(conn).delete_period(year_month, account_id)

        if account_id:
            conn.execute(
                "DELETE FROM account_snapshots WHERE year_month=? AND account_id=?",
                (year_month, account_id)
            )
        else:
            conn.execute("DELETE FROM account_snapshots WHERE year_month=?", (year_month,))

        conn.commit()

    log.warning("RESET-PERIOD: deleted %d transaction(s) for %s%s",
                deleted, year_month, f" account_id={account_id}" if account_id else "")

    acct_label = f" for account {account_id}" if account_id else ""
    msg = f"Deleted {deleted} transaction(s) for {year_month}{acct_label}."
    return redirect(url_for("reset_data_view", msg=msg))


@app.route("/hide-period", methods=["POST"])
def hide_period():
    """
    Excludes a calendar month from every view/report WITHOUT deleting the
    underlying rows — unlike /reset-period, which is a hard delete. Useful
    for "start tracking from month X" without losing earlier statement data
    on file, or for hiding a period known to have bad/anomalous data while
    it's investigated.
    """
    year_month = request.form.get("year_month", "").strip()
    if not year_month:
        return redirect(url_for("reset_data_view"))

    with closing(get_connection()) as conn:
        hidden_txns = TransactionRepository(conn).hide_month(year_month)
        hidden_snaps = SnapshotRepository(conn).hide_month(year_month)
        conn.commit()

    log.warning("HIDE-PERIOD: hid %d transaction(s), %d snapshot(s) for %s",
                hidden_txns, hidden_snaps, year_month)

    msg = f"Hid {hidden_txns} transaction(s) and {hidden_snaps} snapshot(s) for {year_month}. Data is kept, just excluded from views."
    return redirect(url_for("reset_data_view", msg=msg))


@app.route("/vendor-rules/add", methods=["POST"])
def add_vendor_rule():
    pattern     = request.form.get("pattern", "").strip()
    match_type  = request.form.get("match_type", "contains")
    category_id = request.form.get("category_id") or None
    money_type  = request.form.get("money_type", "expense")
    confidence  = request.form.get("confidence", "high")

    if pattern:
        with closing(get_connection()) as conn:
            VendorRuleRepository(conn).add_rule(
                pattern, match_type, category_id, money_type, confidence
            )
            conn.commit()

    return redirect(url_for("vendor_rules_view"))


@app.route("/vendor-rules/<int:rule_id>/delete", methods=["POST"])
def delete_vendor_rule(rule_id):
    with closing(get_connection()) as conn:
        VendorRuleRepository(conn).deactivate(rule_id)
        conn.commit()
        log.warning("VENDOR-RULE-DELETE: rule_id=%d", rule_id)
    return redirect(url_for("vendor_rules_view"))


@app.route("/vendor-rules/<int:rule_id>/approve", methods=["POST"])
def approve_vendor_rule(rule_id):
    with closing(get_connection()) as conn:
        VendorRuleRepository(conn).approve(rule_id)
        conn.commit()
    return redirect(url_for("vendor_rules_view"))


@app.route("/vendor-rules/<int:rule_id>/reject", methods=["POST"])
def reject_vendor_rule(rule_id):
    with closing(get_connection()) as conn:
        VendorRuleRepository(conn).deactivate(rule_id)
        conn.commit()
        log.warning("VENDOR-RULE-REJECT: rule_id=%d", rule_id)
    return redirect(url_for("vendor_rules_view"))


@app.route("/api/vendor-rules/bulk-approve", methods=["POST"])
def api_bulk_approve_vendor_rules():
    data     = request.get_json() or {}
    rule_ids = data.get("rule_ids") or []
    if not rule_ids:
        return jsonify({"ok": False, "error": "No rules selected"}), 400
    with closing(get_connection()) as conn:
        repo = VendorRuleRepository(conn)
        for rule_id in rule_ids:
            repo.approve(rule_id)
        conn.commit()
    return jsonify({"ok": True, "approved": len(rule_ids)})


@app.route("/api/vendor-rules/bulk-delete", methods=["POST"])
def api_bulk_delete_vendor_rules():
    data     = request.get_json() or {}
    rule_ids = data.get("rule_ids") or []
    if not rule_ids:
        return jsonify({"ok": False, "error": "No rules selected"}), 400
    with closing(get_connection()) as conn:
        repo = VendorRuleRepository(conn)
        for rule_id in rule_ids:
            repo.deactivate(rule_id)
        conn.commit()
        log.warning("VENDOR-RULES-BULK-DELETE: %d rule(s): %s", len(rule_ids), rule_ids)
    return jsonify({"ok": True, "removed": len(rule_ids)})


if __name__ == "__main__":
    # Debug mode exposes the Werkzeug interactive debugger (arbitrary code
    # execution) — it must never default on. Enable locally with
    # PULSE_DEBUG=1. For production use a WSGI server (see README):
    #   gunicorn -w 2 -b 127.0.0.1:8000 web.app:app
    app.run(
        debug=os.environ.get("PULSE_DEBUG") == "1",
        host=os.environ.get("PULSE_HOST", "127.0.0.1"),
        port=int(os.environ.get("PULSE_PORT", "5001")),
    )
