"""
Minimal session-token CSRF protection (no new dependencies).

Why this matters for a localhost app with no auth: any webpage the user has
open can POST to 127.0.0.1 (including /reset-data, which wipes the ledger).
The browser will happily send that request; what it CANNOT do cross-origin is
read our pages, so a token embedded in our HTML and required on every POST
blocks the attack.

Enforcement is skipped under app.config["TESTING"] so route tests don't need
token plumbing; tests/test_csrf.py flips TESTING off to test the mechanism.
"""
import hmac
import secrets

from flask import request, session, jsonify


def init_csrf(app):
    @app.before_request
    def _check_csrf():
        if request.method != "POST" or app.config.get("TESTING"):
            return None
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        good = session.get("_csrf_token", "")
        if good and sent and hmac.compare_digest(sent, good):
            return None
        return jsonify({"ok": False, "error": "CSRF token missing or invalid — reload the page and retry."}), 403

    @app.context_processor
    def _inject_csrf_token():
        return {"csrf_token": _get_token}


def _get_token() -> str:
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(32)
    return session["_csrf_token"]
