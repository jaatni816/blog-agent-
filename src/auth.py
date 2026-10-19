"""Google OAuth 2.0 helpers for the Blogger API v3.

At runtime there is no browser: we only need the client id, the client secret
and a long-lived refresh token (created once by `scripts/get_refresh_token.py`).
Google exchanges the refresh token for a short-lived access token for us, and
`google-auth` refreshes it automatically whenever it expires.

Only the Blogger scope is requested: https://www.googleapis.com/auth/blogger
"""

from __future__ import annotations

import logging
from typing import Final

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build

from src.config import (
    BLOGGER_API_NAME,
    BLOGGER_API_VERSION,
    BLOGGER_SCOPE,
    GOOGLE_TOKEN_URI,
    Config,
)

log = logging.getLogger(__name__)

# OAuth errors that Google returns for an expired or revoked refresh token.
_INVALID_GRANT_HINTS: Final[tuple[str, ...]] = (
    "Check GOOGLE_REFRESH_TOKEN in .env is the newest one.",
    "Refresh tokens can be revoked by changing your Google password, by "
    "removing the app from https://myaccount.google.com/connections, or after "
    "7 days when the OAuth consent screen is in 'Testing' mode.",
    "Fix: set the consent screen to 'In production', then run "
    "`python scripts/get_refresh_token.py` again.",
)


class AuthError(RuntimeError):
    """Base error for anything Google-auth related."""


class TokenRefreshError(AuthError):
    """Raised when the stored refresh token can no longer be used."""


def build_credentials(config: Config) -> Credentials:
    """Create Google credentials from environment variables.

    No network call and no browser happen here.
    """
    return Credentials(
        token=None,  # we only hold a refresh token
        refresh_token=config.google_refresh_token,
        client_id=config.google_client_id,
        client_secret=config.google_client_secret,
        token_uri=GOOGLE_TOKEN_URI,
        scopes=[BLOGGER_SCOPE],
        quota_project_id=None,
    )


def ensure_valid_credentials(credentials: Credentials) -> Credentials:
    """Refresh the access token if needed and return valid credentials.

    Raises `TokenRefreshError` with a plain-English explanation when Google
    refuses the refresh token.
    """
    if credentials.valid:
        return credentials

    log.info("Refreshing Google access token...")
    try:
        credentials.refresh(Request())
    except RefreshError as exc:
        message = str(exc)
        if "invalid_grant" in message or "Token has been expired or revoked" in message:
            raise TokenRefreshError(
                "Google rejected the refresh token (invalid_grant).\n"
                + "\n".join(f"  - {hint}" for hint in _INVALID_GRANT_HINTS)
            ) from exc
        raise TokenRefreshError(f"Could not refresh the Google token: {exc}") from exc
    except GoogleAuthError as exc:
        raise TokenRefreshError(f"Google auth error: {exc}") from exc

    if not credentials.valid:
        raise TokenRefreshError("Google token refresh returned an invalid token.")
    return credentials


def get_credentials(config: Config) -> Credentials:
    """Build credentials and make sure they are usable right now."""
    return ensure_valid_credentials(build_credentials(config))


def get_blogger_service(config: Config) -> Resource:
    """Return an authorized Blogger API v3 service object.

    `cache_discovery=False` keeps the client from writing cache files, which
    matters on CI runners.
    """
    credentials = get_credentials(config)
    service = build(
        BLOGGER_API_NAME,
        BLOGGER_API_VERSION,
        credentials=credentials,
        cache_discovery=False,
    )
    log.debug("Blogger API service ready (scope: %s)", BLOGGER_SCOPE)
    return service


def verify_access(service: Resource, blog_id: str) -> str:
    """Cheap permission check: read the blog's own metadata.

    Returns the blog title. Raises `AuthError` with a fix when the token lacks
    the Blogger scope or the user has no access to that blog.
    """
    from googleapiclient.errors import HttpError  # local import: keeps module import cheap

    try:
        blog = service.blogs().get(blogId=blog_id, fields="id,name,url").execute()
    except HttpError as exc:
        status = getattr(exc.resp, "status", None)
        reason = (getattr(exc, "reason", "") or "").lower()
        if status == 403:
            if "insufficient" in reason or "scope" in reason:
                raise AuthError(
                    "403 - the Google token does not have the Blogger scope.\n"
                    "  - Re-run `python scripts/get_refresh_token.py` and make sure "
                    f"the scope used is {BLOGGER_SCOPE}.\n"
                    "  - The token must belong to the Google account that owns the blog."
                ) from exc
            raise AuthError(
                "403 - this Google account cannot edit that blog id. "
                "Check BLOG_ID and make sure the blog is on the same account."
            ) from exc
        if status == 404:
            raise AuthError(
                "404 - Blogger does not know this blog id. Check BLOG_ID."
            ) from exc
        raise AuthError(f"Blogger API error while checking access: {exc}") from exc

    title = blog.get("name", "<unknown title>")
    log.info("Connected to Blogger blog: %s (id ending ...%s)", title, blog_id[-4:])
    return title