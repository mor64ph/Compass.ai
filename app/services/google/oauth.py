"""Google OAuth for the user's own Gmail and Calendar (PRD Epic E).

This is the one integration in Compass with no third-party-data question
attached: it reads the user's own mailbox, under their own consent, via
Google's documented API. Setup steps are in docs/GOOGLE_OAUTH_SETUP.md.

Scopes are kept to the minimum that makes the feature work:

* `gmail.readonly` - classify incoming mail. Compass never sends, deletes or
  modifies a message.
* `calendar.events` - place interview slots. Write access is required to create
  an event; it cannot see or touch calendars beyond events it can access.
"""

from __future__ import annotations

import json
import logging
import os
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import GoogleToken

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

# The redirect target is http://localhost, which oauthlib refuses by default.
# Google explicitly allows plain-HTTP loopback redirects, and Phase 1 runs only
# on the user's own machine - so this is scoped to exactly that case.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
# Google returns scopes in its own order and sometimes adds granted ones; strict
# comparison would raise on an otherwise successful exchange.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


class GoogleNotConfigured(RuntimeError):
    pass


def _client_config() -> dict:
    settings = get_settings()
    if settings.compass_google_client_id and settings.compass_google_client_secret:
        return {
            "web": {
                "client_id": settings.compass_google_client_id,
                "client_secret": settings.compass_google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.compass_google_redirect_uri],
            }
        }

    path = settings.google_client_secrets_path
    if not path.is_file():
        raise GoogleNotConfigured(
            f"No Google OAuth client found. Expected a client-secrets JSON at "
            f"{path}, or COMPASS_GOOGLE_CLIENT_ID / "
            f"COMPASS_GOOGLE_CLIENT_SECRET in .env. "
            f"See docs/GOOGLE_OAUTH_SETUP.md."
        )
    with path.open(encoding="utf-8") as fh:
        config = json.load(fh)
    # A "Desktop app" credential downloads under an "installed" key; normalise so
    # the rest of the code does not care which type was created.
    if "installed" in config and "web" not in config:
        config["web"] = config.pop("installed")
    return config


def _build_flow(state: str | None = None):
    from google_auth_oauthlib.flow import Flow

    settings = get_settings()
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, state=state)
    flow.redirect_uri = settings.compass_google_redirect_uri
    return flow


def authorization_url() -> tuple[str, str]:
    """Returns `(url, state)`. Store the state in the session cookie and verify
    it on callback - it is the CSRF defence for the redirect."""
    flow = _build_flow()
    state = secrets.token_urlsafe(24)
    flow.state = state
    url, returned_state = flow.authorization_url(
        access_type="offline",       # we need a refresh token for background sync
        include_granted_scopes="true",
        prompt="consent",            # forces a refresh token even on re-auth
        state=state,
    )
    return url, returned_state


def exchange_code(
    session: Session, user_id: int, *, code: str, state: str
) -> GoogleToken:
    flow = _build_flow(state=state)
    flow.fetch_token(code=code)
    credentials = flow.credentials
    return _store(session, user_id, credentials)


def _token_row(session: Session, user_id: int) -> GoogleToken | None:
    return session.scalars(
        select(GoogleToken).where(GoogleToken.user_id == user_id)
    ).first()


def _store(session: Session, user_id: int, credentials) -> GoogleToken:
    row = _token_row(session, user_id)
    payload = credentials.to_json()
    if row is None:
        row = GoogleToken(
            user_id=user_id,
            token_json=payload,
            scopes=list(credentials.scopes or SCOPES),
        )
        session.add(row)
    else:
        row.token_json = payload
        row.scopes = list(credentials.scopes or SCOPES)
    session.commit()
    session.refresh(row)
    return row


def load_credentials(session: Session, user_id: int):
    """Return this user's live credentials, refreshing and re-persisting when
    expired. Per-user is not optional: a shared token would mean one person's
    inbox being read on another's behalf."""
    row = _token_row(session, user_id)
    if row is None:
        return None

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    try:
        credentials = Credentials.from_authorized_user_info(json.loads(row.token_json), SCOPES)
    except Exception as exc:
        logger.warning("Stored Google token is unreadable: %s", exc)
        return None

    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            row.token_json = credentials.to_json()
            session.commit()
        except Exception as exc:
            logger.warning("Google token refresh failed: %s", exc)
            return None
    return credentials if credentials.valid else None


def status(session: Session, user_id: int) -> dict:
    settings = get_settings()
    row = _token_row(session, user_id)
    return {
        "configured": settings.google_configured(),
        "connected": row is not None,
        "account_email": row.account_email if row else "",
        "scopes": row.scopes if row else [],
        "connected_at": row.created_at if row else None,
        "secrets_path": str(settings.google_client_secrets_path),
        "redirect_uri": settings.compass_google_redirect_uri,
    }


def disconnect(session: Session, user_id: int) -> None:
    row = _token_row(session, user_id)
    if row is not None:
        session.delete(row)
        session.commit()


def service(session: Session, user_id: int, name: str, version: str):
    """Build a Google API client for this user, or None when not connected."""
    credentials = load_credentials(session, user_id)
    if credentials is None:
        return None
    from googleapiclient.discovery import build

    return build(name, version, credentials=credentials, cache_discovery=False)
