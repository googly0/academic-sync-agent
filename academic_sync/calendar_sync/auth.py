"""Google OAuth: turn credentials on disk into API clients.

Flow (standard installed-app / desktop OAuth):

1. First run reads ``credentials.json`` (downloaded from Google Cloud Console)
   and opens a browser for consent.
2. The resulting access + refresh tokens are cached in ``token.json``.
3. Later runs load ``token.json`` and refresh silently; no browser.

Scopes are the narrowest that do the job, and each caller asks only for what it
needs:

* ``calendar.events`` — manage events, **not** create, share, or delete
  calendars. All the CLI ever requests.
* ``gmail.readonly`` — read course emails. Requested only by the web app's
  Connect Google button, never by the CLI, and it cannot send, delete, or
  label anything.

One ``token.json`` holds whatever has been granted. A token that lacks a scope
a caller needs is treated like no token: re-consent, asking for the union of
what was already granted and what is now needed, so connecting Gmail never
silently drops Calendar access.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .errors import CalendarAuthError

logger = logging.getLogger(__name__)

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

#: What the CLI requests. Unchanged from before Gmail support, so existing
#: token.json files keep working without re-consent.
SCOPES: List[str] = [CALENDAR_SCOPE]

#: What the web app's Connect Google button requests.
APP_SCOPES: List[str] = [CALENDAR_SCOPE, GMAIL_SCOPE]

#: How long a browser consent may take before the flow gives up, so a tab the
#: user closed does not leave a worker thread waiting forever.
CONSENT_TIMEOUT_SECONDS = 300


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
    creds = load_google_credentials(
        SCOPES,
        credentials_path=credentials_path,
        token_path=token_path,
        allow_interactive=allow_interactive,
    )
    return _build("calendar", "v3", creds)


def build_gmail_service(
    *,
    credentials_path: str | Path = "credentials.json",
    token_path: str | Path = "token.json",
    allow_interactive: bool = False,
) -> Any:
    """Return an authorised, read-only Gmail API service object."""
    creds = load_google_credentials(
        [GMAIL_SCOPE],
        credentials_path=credentials_path,
        token_path=token_path,
        allow_interactive=allow_interactive,
    )
    return _build("gmail", "v1", creds)


def load_google_credentials(
    scopes: Sequence[str],
    *,
    credentials_path: str | Path = "credentials.json",
    token_path: str | Path = "token.json",
    allow_interactive: bool = True,
) -> Any:
    """Return valid credentials carrying at least ``scopes``.

    Raises:
        CalendarAuthError: with a message that says what to do next.
    """
    try:
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover - environment problem
        raise CalendarAuthError(
            "Google API client libraries are not installed; "
            "run `pip install -r requirements.txt`"
        ) from exc

    credentials_file = Path(credentials_path).expanduser()
    token_file = Path(token_path).expanduser()
    creds = _load_token(token_file)
    granted: List[str] = list(getattr(creds, "scopes", None) or []) if creds else []

    if creds is not None and not creds.has_scopes(list(scopes)):
        logger.info("cached token lacks %s; re-consent needed", sorted(set(scopes) - set(granted)))
        creds = None

    if creds and creds.valid:
        return creds

    # Silent refresh: the common path on every run after the first.
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _persist(creds, token_file)
            return creds
        except Exception as exc:
            logger.warning("token refresh failed (%s); falling back to consent flow", exc)
            creds = None

    if not allow_interactive:
        raise CalendarAuthError(
            f"no valid cached credentials at {token_file} with the access this needs, "
            "and interactive consent is disabled. Connect your Google account first "
            "(the web app's Connections page, or one CLI run on a machine with a browser)."
        )

    if not credentials_file.exists():
        raise CalendarAuthError(
            f"OAuth client secrets not found at {credentials_file}. Create an OAuth "
            "client ID of type 'Desktop app' in Google Cloud Console, enable the "
            "Google Calendar API (and the Gmail API for email import), and download "
            "the JSON to that path. See the README for step-by-step setup."
        )

    # Ask for everything already granted plus what is needed now, so this
    # consent never narrows an existing token.
    wanted = sorted(set(granted) | set(scopes))
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), wanted)
        creds = flow.run_local_server(port=0, timeout_seconds=CONSENT_TIMEOUT_SECONDS)
    except Exception as exc:
        raise CalendarAuthError(f"OAuth consent flow failed: {exc}") from exc
    if creds is None:
        raise CalendarAuthError("the Google consent page was not completed in time")

    _persist(creds, token_file)
    return creds


def google_status(
    *,
    credentials_path: str | Path = "credentials.json",
    token_path: str | Path = "token.json",
) -> Dict[str, Any]:
    """What the Connections page shows. Never opens a browser, never raises."""
    token_file = Path(token_path).expanduser()
    status: Dict[str, Any] = {
        "client_configured": Path(credentials_path).expanduser().exists(),
        "connected": False,
        "calendar": False,
        "gmail": False,
    }
    try:
        creds = _load_token(token_file)
    except CalendarAuthError as exc:
        status["error"] = str(exc)
        return status
    if creds is None:
        return status
    status["connected"] = bool(creds.refresh_token or creds.valid)
    status["calendar"] = creds.has_scopes([CALENDAR_SCOPE])
    status["gmail"] = creds.has_scopes([GMAIL_SCOPE])
    return status


def _load_token(token_file: Path) -> Optional[Any]:
    if not token_file.exists():
        return None
    from google.oauth2.credentials import Credentials

    try:
        # No ``scopes`` argument: use what the file says was granted. Passing
        # the scopes we *want* would make a refresh request scopes the user
        # never granted, and Google rejects that refresh outright.
        return Credentials.from_authorized_user_file(str(token_file))
    except Exception as exc:
        # A corrupt token is recoverable — delete it and re-consent — so
        # say so rather than dying with a stack trace.
        raise CalendarAuthError(
            f"cached token {token_file} is unreadable ({exc}). "
            f"Delete it and re-run to re-authorise."
        ) from exc


def _build(api: str, version: str, creds: Any) -> Any:
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - environment problem
        raise CalendarAuthError(
            "Google API client libraries are not installed; "
            "run `pip install -r requirements.txt`"
        ) from exc
    return build(api, version, credentials=creds, cache_discovery=False)


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
