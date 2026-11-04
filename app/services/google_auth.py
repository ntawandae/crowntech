
"""
Google OAuth 2.0 authentication using authorization-code flow.

Flow:
1. Redirect user to Google.
2. Google redirects back with an authorization code.
3. Exchange the code for an access token server-side.
4. Retrieve the user's profile from Google's UserInfo endpoint.
5. Verify the email address.

Never trust user identity information submitted by the browser.
"""

from urllib.parse import urlencode

import requests
from flask import current_app


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleAuthError(Exception):
    """Raised when Google authentication fails."""
    pass


def build_auth_url(client_id, redirect_uri, state):
    """
    Build Google's OAuth authorization URL.
    """

    if not client_id or not redirect_uri or not state:
        raise GoogleAuthError(
            "Google authentication configuration is incomplete."
        )

    return AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    })


def fetch_profile(
    client_id,
    client_secret,
    redirect_uri,
    code,
    http=None,
    timeout=20
):
    """
    Exchange Google's authorization code for an access token
    and retrieve the verified Google user profile.

    Returns:
        {
            "sub": Google unique user ID,
            "email": verified email address,
            "name": user's Google display name
        }

    Raises:
        GoogleAuthError
    """

    http = http or requests

    # --------------------------------------------------
    # Validate configuration
    # --------------------------------------------------

    if not client_id:
        raise GoogleAuthError("Google Client ID is missing.")

    if not client_secret:
        raise GoogleAuthError("Google Client Secret is missing.")

    if not redirect_uri:
        raise GoogleAuthError("Google Redirect URI is missing.")

    if not code:
        raise GoogleAuthError("Google authorization code is missing.")

    # --------------------------------------------------
    # Exchange authorization code for access token
    # --------------------------------------------------

    try:

        tok = http.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=timeout,
        )

    except requests.RequestException:

        current_app.logger.exception(
            "Google token endpoint connection failed."
        )

        raise GoogleAuthError(
            "Could not connect to Google. Please try again."
        )

    # --------------------------------------------------
    # Read token response
    # --------------------------------------------------

    try:
        token_data = tok.json()

    except ValueError:

        current_app.logger.error(
            "Google returned an invalid token response. HTTP %s",
            tok.status_code
        )

        raise GoogleAuthError(
            "Google returned an invalid response."
        )

    # --------------------------------------------------
    # Handle Google token errors
    # --------------------------------------------------

    if tok.status_code != 200:

        error = token_data.get("error", "unknown")
        description = token_data.get(
            "error_description",
            "No description provided."
        )

        # Do not log the authorization code, client secret,
        # access token or full token response.

        current_app.logger.error(
            "GOOGLE TOKEN ERROR | HTTP %s | ERROR: %s | DESCRIPTION: %s",
            tok.status_code,
            error,
            description
        )

        if error == "invalid_grant":
            message = (
                "Google authorization expired or was already used. "
                "Please try signing in again."
            )

        elif error == "invalid_client":
            message = (
                "Google authentication configuration is incorrect."
            )

        elif error == "unauthorized_client":
            message = (
                "Google authentication is not authorized."
            )

        else:
            message = (
                "Google did not accept the sign-in. Please try again."
            )

        raise GoogleAuthError(message)

    # --------------------------------------------------
    # Extract access token
    # --------------------------------------------------

    access_token = token_data.get("access_token")

    if not access_token:

        current_app.logger.error(
            "Google token response did not contain access_token."
        )

        raise GoogleAuthError(
            "Google did not provide an access token."
        )

    # --------------------------------------------------
    # Retrieve Google user profile
    # --------------------------------------------------

    try:

        info = http.get(
            USERINFO_URL,
            headers={
                "Authorization": f"Bearer {access_token}"
            },
            timeout=timeout,
        )

    except requests.RequestException:

        current_app.logger.exception(
            "Google UserInfo endpoint connection failed."
        )

        raise GoogleAuthError(
            "Could not retrieve your Google profile."
        )

    # --------------------------------------------------
    # Validate profile response
    # --------------------------------------------------

    if info.status_code != 200:

        current_app.logger.error(
            "Google UserInfo failed with HTTP %s.",
            info.status_code
        )

        raise GoogleAuthError(
            "Could not read your Google profile. Please try again."
        )

    try:
        data = info.json()

    except ValueError:

        current_app.logger.error(
            "Google UserInfo returned invalid JSON."
        )

        raise GoogleAuthError(
            "Google returned an invalid profile response."
        )

    # --------------------------------------------------
    # Validate Google identity
    # --------------------------------------------------

    google_id = data.get("sub")

    email = (
        data.get("email") or ""
    ).strip().lower()

    email_verified = data.get("email_verified")

    if not google_id or not email:

        raise GoogleAuthError(
            "Google did not share a valid email address."
        )

    if email_verified not in (True, "true"):

        raise GoogleAuthError(
            "Your Google email address is not verified."
        )

    # --------------------------------------------------
    # Return verified Google profile
    # --------------------------------------------------

    return {
        "sub": str(google_id),
        "email": email,
        "name": (
            data.get("name") or email.split("@")[0]
        )[:100],
    }
