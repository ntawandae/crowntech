"""
Supabase Storage client (plain REST, using `requests` - no extra library).

Two buckets are used:
  * SUPABASE_PRODUCT_BUCKET (default "product-images")  - PUBLIC. Product photos, shown in the shop.
  * SUPABASE_DOCS_BUCKET    (default "staff-documents") - PRIVATE. Staff contracts, opened only through a
    short-lived signed link that the app hands to the owner or an admin.

Storage is switched on by setting SUPABASE_URL and SUPABASE_SERVICE_KEY. Without them the app keeps using
the local disk exactly as before (handy for development).
The service key can do anything in the project: keep it in the host's environment settings only.
"""
import logging
from urllib.parse import quote

import requests
from flask import current_app

log = logging.getLogger(__name__)
_ensured = {"done": False}


class StorageError(Exception):
    pass


def _cfg(name, default=""):
    return (current_app.config.get(name) or default)


def enabled():
    return bool(_cfg("SUPABASE_URL") and _cfg("SUPABASE_SERVICE_KEY"))


def bucket_products():
    return _cfg("SUPABASE_PRODUCT_BUCKET", "product-images")


def bucket_docs():
    return _cfg("SUPABASE_DOCS_BUCKET", "staff-documents")


def _base():
    return _cfg("SUPABASE_URL").rstrip("/") + "/storage/v1"


def _headers(extra=None):
    key = _cfg("SUPABASE_SERVICE_KEY")
    headers = {"apikey": key}
    if not key.startswith("sb_"):                 # new-style "sb_secret_..." keys are not JWTs: apikey header only
        headers["Authorization"] = f"Bearer {key}"
    if extra:
        headers.update(extra)
    return headers


def _object_url(bucket, path):
    return f"{_base()}/object/{quote(bucket, safe='')}/{quote(path, safe='/')}"


def _check(resp, what):
    if resp.status_code >= 300:
        raise StorageError(f"Supabase storage: {what} failed ({resp.status_code}): {resp.text[:200]}")
    return resp


def public_url(bucket, path):
    """Address anyone can open (only works for a public bucket)."""
    return f"{_base()}/object/public/{quote(bucket, safe='')}/{quote(path, safe='/')}"


def upload(bucket, path, data, content_type="application/octet-stream", cache_seconds=31536000):
    try:
        resp = requests.post(
            _object_url(bucket, path), data=data, timeout=(5, 60),
            headers=_headers({"Content-Type": content_type, "x-upsert": "true",
                              "cache-control": f"max-age={cache_seconds}"}))
    except requests.RequestException as exc:
        raise StorageError(f"Supabase storage: could not reach the server ({exc.__class__.__name__})") from exc
    _check(resp, f"upload of {path}")


def delete(bucket, paths):
    paths = [p for p in paths if p]
    if not paths:
        return
    try:
        resp = requests.delete(f"{_base()}/object/{quote(bucket, safe='')}", json={"prefixes": paths},
                               headers=_headers(), timeout=(5, 30))
    except requests.RequestException as exc:
        raise StorageError(f"Supabase storage: could not reach the server ({exc.__class__.__name__})") from exc
    _check(resp, "delete")


def signed_url(bucket, path, expires=60):
    """A link that opens one private file for `expires` seconds."""
    try:
        resp = requests.post(f"{_base()}/object/sign/{quote(bucket, safe='')}/{quote(path, safe='/')}",
                             json={"expiresIn": int(expires)}, headers=_headers(), timeout=(5, 20))
    except requests.RequestException as exc:
        raise StorageError(f"Supabase storage: could not reach the server ({exc.__class__.__name__})") from exc
    _check(resp, f"signing {path}")
    signed = resp.json().get("signedURL") or resp.json().get("signedUrl")
    if not signed:
        raise StorageError("Supabase storage: no signed link returned")
    return signed if signed.startswith("http") else _base() + (signed if signed.startswith("/") else "/" + signed)


def download_public(bucket, path, timeout=15):
    """Bytes of a file in a public bucket, or None."""
    try:
        resp = requests.get(public_url(bucket, path), timeout=timeout)
        return resp.content if resp.status_code == 200 else None
    except requests.RequestException:
        return None


def ensure_buckets(force=False):
    """
    Create the two buckets if they don't exist yet, and make sure the product bucket is public and the
    documents bucket is private. Runs at most once per process; never raises (uploads will report any real problem).
    """
    if _ensured["done"] and not force:
        return
    _ensured["done"] = True
    wanted = {bucket_products(): True, bucket_docs(): False}
    try:
        resp = requests.get(f"{_base()}/bucket", headers=_headers(), timeout=(5, 20))
        _check(resp, "listing buckets")
        existing = {b.get("id") or b.get("name"): bool(b.get("public")) for b in resp.json()}
        for name, public in wanted.items():
            if name not in existing:
                _check(requests.post(f"{_base()}/bucket", json={"id": name, "name": name, "public": public},
                                     headers=_headers(), timeout=(5, 20)), f"creating bucket {name}")
                log.info("Created Supabase bucket %s (public=%s)", name, public)
            elif existing[name] != public:
                _check(requests.put(f"{_base()}/bucket/{quote(name, safe='')}", json={"public": public},
                                    headers=_headers(), timeout=(5, 20)), f"updating bucket {name}")
                log.warning("Set Supabase bucket %s to public=%s", name, public)
    except (StorageError, requests.RequestException, ValueError) as exc:
        log.warning("Could not check Supabase buckets: %s", exc)
