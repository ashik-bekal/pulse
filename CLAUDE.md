# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

PULSE is a local-first, SQLite-backed personal finance ledger. It parses bank statement PDFs into a reconciled, categorized, multi-currency ledger — no cloud, no accounts. The core invariant: every statement must reconcile to the penny against its own printed balances; parsing gaps surface explicitly rather than being silently absorbed.

## Running and developing

```bash
# Bootstrap demo data and start the server
python3 cli/seed_demo.py        # schema + fictional demo data
python3 web/app.py              # http://127.0.0.1:5001

# Empty ledger instead
python3 cli/init_db.py

# Import statements from CLI
python3 cli/ingest.py hsbc /path/to/statement.pdf
python3 cli/ingest.py chase_bank /path/to/statement.pdf --year 2025 --start-month 5
python3 cli/ingest.py sapphire /path/to/statement.pdf --year 2025 --start-month 5

# Run all tests
python3 -m pytest tests/ -v

# Run a single test file
python3 -m pytest tests/test_ingestion.py -v

# Run a single test by name
python3 -m pytest tests/test_ingestion.py::test_duplicate_detection -v
```

Configuration is via environment variables; see `.env.example`. `PULSE_DB_PATH` redirects the database — tests use `:memory:` via `sqlite3.connect(":memory:")` directly.

## Architecture

```
parsers/        pure text → RawTransaction converters + per-format reconcile()
domain/         models + categorization engine — NO I/O, NO imports from other layers
services/       ingestion (categorize + persist + flag), FX, reconciliation
persistence/    the ONLY place SQL strings live — one repository class per aggregate
web/            Flask routes + templates + async import job queue
cli/            init_db, seed_demo, ingest
tests/          synthetic-fixture suite — no real PDFs or disk DB needed
```

### Key design rules

**`domain/` has no I/O.** `domain/models.py` and `domain/categorization.py` import nothing from `persistence/`, `services/`, or `web/`. This makes them trivially testable.

**All SQL is in `persistence/repositories.py`.** Never write SQL strings in Flask routes, services, or CLIs. Routes open a connection via `persistence.database.get_connection()`, pass it to repository constructors, and call repository methods.

**Parser contract:** Each parser module (`parsers/hsbc.py`, `parsers/chase_bank.py`, `parsers/chase_sapphire.py`) exposes two module-level functions — `parse(text, **context) -> List[RawTransaction]` and `reconcile(transactions, **context) -> ReconciliationResult`. No inheritance; a `StatementParser` Protocol in `parsers/base.py` defines the shape for static typing.

**`RawTransaction` is the universal parser output.** All three parsers emit the same frozen dataclass. `transaction_currency/amount` = what currency the purchase happened in; `settlement_currency/amount` = what hit the account balance. For domestic transactions these are identical.

**Ingestion is a thin wire between categorization and persistence.** `services/ingestion.ingest_transactions()` takes already-parsed `RawTransaction` lists (it doesn't know what a PDF is) and already-loaded repositories, then categorizes and persists. Vendor rules are sorted once per batch (descending pattern length) so longer/more-specific rules win.

**Duplicate detection uses a pre-batch snapshot.** `txn_repo.existing_fingerprint_counts()` is called once before any inserts in a batch. Same-day repeat transactions (e.g. two identical transit top-ups) are legitimate and kept; re-importing the same statement is rejected at the statement level via `already_imported()`.

**The reporting currency is GBP** (`config.REPORTING_CURRENCY`). FX conversion happens at import time via monthly rates stored in `exchange_rates`.

### Web import flow

`web/import_service.py` handles async PDF imports. `detect_pdf()` reads the first 3 pages to identify format and account by account number. `start_job()` spawns a worker thread. Import state is in-memory only — a server restart clears job history, and temp PDFs in `TEMP_DIR` are cleaned up on job completion.

### Testing approach

Tests use `sqlite3.connect(":memory:")` with `persistence.database.init_schema()` applied, then pass the connection to repository constructors directly — no disk DB, no real PDFs. Parser tests use synthetic text fixtures constructed in the test file. The `conn` pytest fixture pattern is the standard approach; see `tests/test_ingestion.py` for reference.
