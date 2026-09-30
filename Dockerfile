FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .
RUN mkdir -p data

ENV PULSE_HOST=0.0.0.0
ENV PULSE_PORT=5001
ENV PULSE_DB_PATH=/app/data/ledger.db

EXPOSE 5001

# First boot (no DB yet): create the schema + starter data. Every boot after
# that: only apply pending migrations. init_db.py seeds demo owner/accounts, so
# it must never run against an existing (possibly real) ledger.
CMD sh -c 'if [ ! -f "$PULSE_DB_PATH" ]; then python3 cli/init_db.py; fi && python3 cli/migrate.py && gunicorn -w 2 -b 0.0.0.0:5001 web.app:app'
