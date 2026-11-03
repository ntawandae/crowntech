"""
Sign in with Google (OAuth 2.0 authorization-code flow) using `requests`.

We never read the ID token ourselves. After Google redirects back with a one-time `code`, our server
exchanges it directly with Google over HTTPS (proving it with the client secret) and then asks Google's
userinfo endpoint who the user is. Everything we trust comes straight from Google, not from the browser.
"""
from urllib.parse import urlencode

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleAuthError(Exception):
    pass


def build_auth_url(client_id, redirect_uri, state):
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    })


def fetch_profile(client_id, client_secret, redirect_uri, code, http=None, timeout=20):
    """Exchange `code` and return {'sub','email','name'} for a Google-verified email. Raises GoogleAuthError."""
    http = http or requests
    try:
        tok = http.post(TOKEN_URL, data={
            "code": code, "client_id": client_id, "client_secret": client_secret,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code",
        }, timeout=timeout)
        if tok.status_code != 200 or "access_token" not in tok.json():
            raise GoogleAuthError("Google did not accept the sign-in. Please try again.")
        info = http.get(USERINFO_URL, headers={"Authorization": f"Bearer {tok.json()['access_token']}"}, timeout=timeout)
    except requests.RequestException as exc:
        raise GoogleAuthError("Could not reach Google. Please try again.") from exc
    if info.status_code != 200:
        raise GoogleAuthError("Could not read your Google profile. Please try again.")
    data = info.json()
    email = (data.get("email") or "").strip().lower()
    if not data.get("sub") or not email:
        raise GoogleAuthError("Google did not share an email address.")
    if data.get("email_verified") not in (True, "true"):
        raise GoogleAuthError("Your Google email address is not verified.")
    return {"sub": str(data["sub"]), "email": email, "name": (data.get("name") or email.split("@")[0])[:100]}
