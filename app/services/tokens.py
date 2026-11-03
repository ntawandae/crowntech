"""One-time email links: create, check and consume. Only a SHA-256 hash of each token is stored."""
import hashlib
import secrets
from datetime import datetime, timedelta

VERIFY = "verify_email"
RESET = "reset_password"


def new_token():
    return secrets.token_urlsafe(32)


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_usable(expires_at, used_at, now=None):
    now = now or datetime.utcnow()
    return used_at is None and expires_at > now


# ---- database helpers (need the app context) ------------------------------
def issue(user, purpose, lifetime):
    """Create a fresh token for `user`, cancelling any earlier unused ones. Returns the raw token."""
    from .. import db
    from ..models import AuthToken
    now = datetime.utcnow()
    for old in AuthToken.query.filter_by(user_id=user.id, purpose=purpose, used_at=None).all():
        old.used_at = now
    raw = new_token()
    db.session.add(AuthToken(user_id=user.id, purpose=purpose, token_hash=hash_token(raw),
                             expires_at=now + lifetime))
    db.session.commit()
    return raw


def find(raw, purpose):
    """The AuthToken row for a raw token if it is still valid, else None."""
    from ..models import AuthToken
    if not raw:
        return None
    row = AuthToken.query.filter_by(token_hash=hash_token(raw), purpose=purpose).first()
    return row if row and is_usable(row.expires_at, row.used_at) else None


def consume(row):
    from .. import db
    row.used_at = datetime.utcnow()
    db.session.commit()
