# PULSE — Personal Unified Ledger & Spending Engine

![Tests](https://github.com/ashik-bekal/pulse/actions/workflows/tests.yml/badge.svg)

Turn bank statement PDFs into a reconciled, categorized, multi-currency
personal ledger — local-first, SQLite-backed, no cloud, no accounts.

The core discipline: **every statement must tie out exactly** against its
own printed balances. If a parsed month doesn't reconcile to the penny,
PULSE says so instead of silently absorbing the gap — so your ledger is
provably complete, not approximately right.

## Features

- **Statement parsing** for three PDF formats: HSBC UK current accounts,
  Chase checking/savings (combined statements split per account), and
  Chase credit cards (incl. three-line FX breakouts with exchange rates)
- **Exact reconciliation** per statement: opening balance walked through
  every transaction must reproduce each printed checkpoint and the
  closing balance; gaps and unverified periods are surfaced on the dashboard
- **Multi-currency**: native, settled, and reporting (GBP) amounts per
  transaction, with monthly exchange rates
- **Auto-categorization** via vendor rules (contains/exact/regex), with a
  review queue for low-confidence matches and auto-suggested rules that
  wait for your approval before being applied
- **Duplicate-safe imports**: re-uploading overlapping statements skips
  what's already present — while same-day repeat purchases are kept
- **Web UI**: dashboard (balances, statement coverage, net worth),
  transactions with filters/bulk actions/running totals, stacked
  spend-by-category analysis with drill-down, trips, and a shared edit
  modal (category/money type/trip — never the statement's own figures)
- **Async PDF import**: drag-and-drop several statements, auto-detection
  of format and target account by account number, background jobs with
  per-file reconciliation status

## Quickstart

```bash
pip install -r requirements.txt
python3 cli/seed_demo.py        # schema + fictional demo data
python3 web/app.py              # http://127.0.0.1:5001
```

Prefer an empty ledger? `python3 cli/init_db.py` instead of the seed.

Or Docker, one command:

```bash
docker compose up
```

Serves at http://127.0.0.1:5001, data persists in `./data`.

Import statements from the UI (Import button) or the CLI:

```bash
python3 cli/ingest.py hsbc /path/to/statement.pdf
python3 cli/ingest.py chase_bank /path/to/statement.pdf --year 2025 --start-month 5
python3 cli/ingest.py sapphire /path/to/statement.pdf --year 2025 --start-month 5
```

## Configuration

All settings are environment variables with safe local defaults — see
[.env.example](.env.example):

| Variable | Default | Purpose |
|---|---|---|
| `PULSE_DB_PATH` | `data/ledger.db` | SQLite location |
| `PULSE_SECRET_KEY` | random per process | Flask secret (set in production) |
| `PULSE_HOST` / `PULSE_PORT` | `127.0.0.1` / `5001` | Dev server bind |
| `PULSE_DEBUG` | `0` | Werkzeug debugger (dev only — it executes code) |
| `PULSE_MAX_UPLOAD_MB` | `16` | Statement upload cap |
| `PULSE_LOG_LEVEL` | `INFO` | Logging verbosity |

## Security model

PULSE is a **single-user, local-first** app: there is no authentication
layer. It binds to `127.0.0.1` by default and should stay there. If you
must reach it remotely, put it behind a reverse proxy that provides
authentication (and TLS), and set `PULSE_SECRET_KEY`.

Your financial data never leaves your machine: no telemetry, no external
calls. The `.gitignore` blocks databases and statement PDFs from ever
being committed.

## Production notes

Run under a WSGI server instead of the dev server:

```bash
pip install gunicorn
PULSE_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))") \
  gunicorn -w 2 -b 127.0.0.1:8000 web.app:app
```

SQLite in WAL mode handles this app's single-user concurrency fine; back
up by copying `data/ledger.db` while the app is idle.

## Architecture

```
parsers/       pure text -> RawTransaction converters + per-format reconcile()
domain/        models + categorization engine (pure functions, no I/O)
services/      ingestion (categorize + persist + flag), FX, reconciliation
persistence/   the ONLY place SQL lives (repository per aggregate)
web/           Flask routes + templates; import job queue
cli/           init_db, seed_demo, ingest
tests/         synthetic-fixture suite (no real statements needed)
```

## Tests

```bash
python3 -m pytest tests/ -v
```

## License

[MIT](LICENSE)
