"""Google Calendar OAuth: turn credentials on disk into an API client.

Flow (standard installed-app / desktop OAuth):

1. First run reads ``credentials.json`` (downloaded from Google Cloud Console)
   and opens a browser for consent.
2. The resulting access + refresh tokens are cached in ``token.json``.
3. Later runs load ``token.json`` and refresh silently; no browser.

The scope is ``calendar.events`` — permission to manage events, **not** to
create, share, or delete calendars. Narrowest scope that does the job.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Optional

from .errors import CalendarAuthError

logger = logging.getLogger(__name__)

#: Read/write access to events only. Changing this invalidates existing
#: token.json files — users will be prompted to re-consent.
SCOPES: List[str] = ["https://www.googleapis.com/auth/calendar.events"]


def build_calendar_service(
    *,
    credentials_path: str | Path = "credentials.json",
    token_path: str | Path = "token.json",
    allow_interactive: bool = True,
) -> Any:
    """Return an authorised Google Calendar API service object.

    Args:
        credentials_path: OAuth *client* secrets from Google Cloud Console.
        token_path: Where the per-user token is cached. Created on first run.
        allow_interactive: When ``False``, refuse to open a browser — for
            servers and cron jobs, where a blocked consent prompt would hang
            forever. Fails fast with an actionable message instead.

    Raises:
        CalendarAuthError: with a message that says what to do next.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - environment problem
        raise CalendarAuthError(
            "Google API client libraries are not installed; "
            "run `pip install -r requirements.txt`"
        ) from exc

    credentials_file = Path(credentials_path).expanduser()
    token_file = Path(token_path).expanduser()
    creds = None

    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        except Exception as exc:
            # A corrupt token is recoverable — delete it and re-consent — so
            # say so rather than dying with a stack trace.
            raise CalendarAuthError(
                f"cached token {token_file} is unreadable ({exc}). "
                f"Delete it and re-run to re-authorise."
            ) from exc

    if creds and creds.valid:
        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    # Silent refresh: the common path on every run after the first.
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _persist(creds, token_file)
            return build("calendar", "v3", credentials=creds, cache_discovery=False)
        except Exception as exc:
            logger.warning("token refresh failed (%s); falling back to consent flow", exc)
            creds = None

    if not allow_interactive:
        raise CalendarAuthError(
            f"no valid cached credentials at {token_file} and interactive consent is "
            "disabled. Run once on a machine with a browser to create token.json, "
            "then copy it to this host."
        )

    if not credentials_file.exists():
        raise CalendarAuthError(
            f"OAuth client secrets not found at {credentials_file}. Create an OAuth "
            "client ID of type 'Desktop app' in Google Cloud Console, enable the "
            "Google Calendar API, and download the JSON to that path. "
            "See the README for step-by-step setup."
        )

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
        creds = flow.run_local_server(port=0)
    except Exception as exc:
        raise CalendarAuthError(f"OAuth consent flow failed: {exc}") from exc

    _persist(creds, token_file)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _persist(creds: Any, token_file: Path) -> None:
    """Cache the token with owner-only permissions.

    It is a live credential; 0600 keeps it out of reach of other local users.
    """
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    try:
        token_file.chmod(0o600)
    except OSError:  # pragma: no cover - non-POSIX filesystems
        logger.debug("could not chmod %s; continuing", token_file)
