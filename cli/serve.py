"""
Run PULSE under waitress, a production WSGI server that works on Windows
(gunicorn does not). This is what the always-on server runs; for
development keep using `python web/app.py` (or scripts/windows/dev.ps1).

Binds to 127.0.0.1 by default. PULSE has no login, so remote access should
come through something that authenticates for you (e.g. Tailscale Serve
proxying to this loopback port), never by binding to a public interface.

Run: python cli/serve.py
     python cli/serve.py --port 5001 --db-path data/ledger.db --log-file data/logs/server.log
"""
import argparse
import logging
import logging.handlers
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _configure_file_logging(path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)


def main():
    parser = argparse.ArgumentParser(description="Serve PULSE with waitress")
    parser.add_argument("--host", default=os.environ.get("PULSE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PULSE_PORT", "5001")))
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--db-path", default=None,
                        help="ledger.db to serve (overrides PULSE_DB_PATH for this process)")
    parser.add_argument("--log-file", default=None,
                        help="also write logs here (rotated at 2 MB, 3 kept)")
    args = parser.parse_args()

    if args.db_path:
        # Must be set before web.app is imported: it resolves the DB path
        # (and the .secret_key beside it) at import time.
        os.environ["PULSE_DB_PATH"] = os.path.abspath(args.db_path)

    from waitress import serve
    from persistence.database import get_db_path
    from web.app import app  # runs logging.basicConfig — must come before we add a handler

    if args.log_file:
        _configure_file_logging(args.log_file)

    log = logging.getLogger("pulse.serve")
    log.info("PULSE serving http://%s:%d (db: %s)", args.host, args.port,
             os.path.abspath(get_db_path()))
    serve(app, host=args.host, port=args.port, threads=args.threads)


if __name__ == "__main__":
    main()
