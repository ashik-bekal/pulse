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

# Init schema on first boot if the DB doesn't exist yet, then serve.
CMD sh -c "python3 cli/init_db.py && gunicorn -w 2 -b 0.0.0.0:5001 web.app:app"
