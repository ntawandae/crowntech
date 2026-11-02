"""Small security helpers: CSRF tokens for the new forms, login throttling, safe redirects."""
import hmac
import secrets
import time
from collections import defaultdict

from flask import abort, request, session


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


def protect_blueprint(bp, exempt=()):
    """Require a valid CSRF token on every POST to this blueprint (except `exempt` endpoints)."""
    @bp.before_request
    def _check_csrf():
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.endpoint not in exempt:
            sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
            expected = session.get("_csrf", "")
            if not expected or not sent or not hmac.compare_digest(sent, expected):
                abort(400, "Your session expired. Please go back, refresh the page and try again.")


def safe_next(url, fallback="/"):
    """Only allow redirects to paths on this site."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return fallback


# --- login throttling (per process; resets on restart) ----------------------
_failures = defaultdict(list)


def throttled(key, limit=6, window=600):
    now = time.time()
    _failures[key] = [t for t in _failures[key] if now - t < window]
    return len(_failures[key]) >= limit


def record_failure(key):
    _failures[key].append(time.time())


def clear_failures(key):
    _failures.pop(key, None)


def client_key(identity):
    return f"{request.remote_addr}|{(identity or '').lower()}"
