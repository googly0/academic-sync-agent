"""Hosted sign-in: Google OAuth for one allow-listed person.

Locally the server only listens on localhost and has no login. Deployed, it is
on a public URL and can write to a calendar and spend an API key, so every
request must come from a signed-in session belonging to ``ALLOWED_EMAIL``.

How it fits together
--------------------
* **One consent, three jobs.** ``/auth/login`` sends the browser to Google
  asking for sign-in *and* Calendar *and* Gmail. The refresh token that comes
  back is stored (encrypted) so the server can act on those APIs later,
  without the user present. This replaces the desktop-style
  ``run_local_server`` flow, which opens a browser on the *server* and so
  cannot work remotely.
* **Allow-list, checked on the verified ID token.** The email comes from
  Google's signed ID token and must have ``email_verified``. Anyone else —
  including a perfectly valid Google account — gets a 403 and nothing is
  stored.
* **Stateless sessions.** A signed, HttpOnly, SameSite=Lax cookie. Serverless
  instances share no memory, so nothing about a session may live in process.
* **CSRF token derived from the session.** Mutating requests must send
  ``X-App-Token`` = HMAC(session id). Any instance can recompute and check it,
  no store needed; a foreign page can neither read it nor set the header.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..calendar_sync.auth import APP_SCOPES

logger = logging.getLogger(__name__)

SESSION_COOKIE = "as_session"
TX_COOKIE = "as_oauth"
SESSION_MAX_AGE = 30 * 24 * 3600
TX_MAX_AGE = 10 * 60
TOKEN_HEADER = "x-app-token"

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
LOGIN_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email", *APP_SCOPES]

#: Paths reachable without a session. Static assets hold no data; /auth/* is
#: how a session is obtained.
PUBLIC_PREFIXES = ("/auth/", "/static/")


class HostedConfigError(RuntimeError):
    """A required environment variable is missing. Names every one that is."""


@dataclass(frozen=True)
class HostedSettings:
    allowed_email: str
    session_secret: str
    client_id: str
    client_secret: str
    #: Public base URL, e.g. https://semester-sync.vercel.app. Optional: when
    #: unset it is derived from the request, which works behind Vercel's proxy.
    app_url: Optional[str] = None

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "HostedSettings":
        env = os.environ if env is None else env
        required = ("ALLOWED_EMAIL", "SESSION_SECRET", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
        missing = [name for name in required if not env.get(name)]
        if missing:
            raise HostedConfigError(
                "missing environment variable(s): " + ", ".join(missing)
            )
        if len(env["SESSION_SECRET"]) < 32:
            raise HostedConfigError(
                "SESSION_SECRET is too short — use at least 32 random characters "
                "(python -c \"import secrets; print(secrets.token_urlsafe(48))\")"
            )
        return cls(
            allowed_email=env["ALLOWED_EMAIL"].strip().lower(),
            session_secret=env["SESSION_SECRET"],
            client_id=env["GOOGLE_CLIENT_ID"],
            client_secret=env["GOOGLE_CLIENT_SECRET"],
            app_url=(env.get("APP_URL") or "").rstrip("/") or None,
        )


def csrf_token(secret: str, session_id: str) -> str:
    """The request token for a session. Deterministic, so any instance agrees."""
    return hmac.new(secret.encode(), f"csrf:{session_id}".encode(), hashlib.sha256).hexdigest()


class HostedAuth:
    """Sessions, the OAuth round-trip, and the request guard."""

    def __init__(
        self,
        settings: HostedSettings,
        token_store: Any,
        *,
        exchange: Optional[Callable[[str, str, str], Tuple[Any, Dict[str, Any]]]] = None,
    ) -> None:
        """
        Args:
            token_store: where the Google refresh token is saved
                (a ``DbTokenStore``).
            exchange: ``(redirect_uri, code, code_verifier) -> (credentials,
                id_token_claims)``. Defaults to the real Google exchange;
                replaceable so tests need no network.
        """
        self.settings = settings
        self.token_store = token_store
        self._exchange = exchange or self._google_exchange
        self._sessions = URLSafeTimedSerializer(settings.session_secret, salt="session")
        self._tx = URLSafeTimedSerializer(settings.session_secret, salt="oauth-tx")

    # -- sessions ------------------------------------------------------------

    def read_session(self, request: Request) -> Optional[Dict[str, Any]]:
        raw = request.cookies.get(SESSION_COOKIE)
        if not raw:
            return None
        try:
            data = self._sessions.loads(raw, max_age=SESSION_MAX_AGE)
        except (BadSignature, SignatureExpired):
            return None
        # Re-check the allow-list on every request, so removing someone from
        # ALLOWED_EMAIL takes effect without waiting for their cookie to expire.
        if not isinstance(data, dict) or data.get("email") != self.settings.allowed_email:
            return None
        return data

    def token_for(self, session: Dict[str, Any]) -> str:
        return csrf_token(self.settings.session_secret, session["sid"])

    def _set_session(self, response: Any, request: Request, email: str) -> None:
        value = self._sessions.dumps({"email": email, "sid": secrets.token_urlsafe(16)})
        response.set_cookie(
            SESSION_COOKIE, value, max_age=SESSION_MAX_AGE, httponly=True,
            secure=_is_https(request), samesite="lax", path="/",
        )

    # -- request guard (installed as middleware) -------------------------------

    async def guard(self, request: Request, call_next: Any) -> Any:
        path = request.url.path
        if path.startswith(PUBLIC_PREFIXES) and path != "/auth/logout":
            return await call_next(request)

        session = self.read_session(request)
        if session is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "sign in required"}, status_code=401)
            if path == "/":
                return HTMLResponse(LOGIN_PAGE)
            return RedirectResponse("/")

        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            sent = request.headers.get(TOKEN_HEADER, "")
            if not hmac.compare_digest(sent, self.token_for(session)):
                return JSONResponse({"detail": "missing or invalid app token"}, status_code=403)
        request.state.session = session
        return await call_next(request)

    # -- routes ----------------------------------------------------------------

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/auth/login")
        def login(request: Request) -> Any:
            from google_auth_oauthlib.flow import Flow

            flow = Flow.from_client_config(self._client_config(), scopes=LOGIN_SCOPES)
            flow.redirect_uri = self._redirect_uri(request)
            # offline + consent: a refresh token is only issued on a fresh
            # consent, and the server needs one to act when nobody is signed in.
            url, state = flow.authorization_url(
                access_type="offline",
                prompt="consent",
                include_granted_scopes="true",
                login_hint=self.settings.allowed_email,
            )
            response = RedirectResponse(url)
            response.set_cookie(
                TX_COOKIE,
                self._tx.dumps({"state": state, "verifier": flow.code_verifier}),
                max_age=TX_MAX_AGE, httponly=True, secure=_is_https(request),
                samesite="lax", path="/auth/",
            )
            return response

        @router.get("/auth/callback")
        def callback(request: Request, code: str = "", state: str = "", error: str = "") -> Any:
            if error:
                return HTMLResponse(_message_page("Sign-in cancelled", error), status_code=400)
            try:
                tx = self._tx.loads(request.cookies.get(TX_COOKIE, ""), max_age=TX_MAX_AGE)
            except (BadSignature, SignatureExpired):
                return HTMLResponse(
                    _message_page("Sign-in expired", "Start again from the sign-in page."),
                    status_code=400,
                )
            if not code or not secrets.compare_digest(str(tx.get("state")), state):
                return HTMLResponse(
                    _message_page("Sign-in failed", "The request could not be verified."),
                    status_code=400,
                )

            try:
                creds, claims = self._exchange(
                    self._redirect_uri(request), code, tx.get("verifier") or ""
                )
            except Exception as exc:
                logger.warning("google sign-in exchange failed: %s", exc)
                return HTMLResponse(
                    _message_page("Sign-in failed", "Google rejected the sign-in. Try again."),
                    status_code=400,
                )

            email = str(claims.get("email", "")).lower()
            if not claims.get("email_verified") or email != self.settings.allowed_email:
                # Nothing is stored for a stranger — not even their token.
                logger.warning("sign-in refused for %s", email or "<no email>")
                return HTMLResponse(
                    _message_page("Not allowed", "This app is private. That account isn't on its list."),
                    status_code=403,
                )

            if getattr(creds, "refresh_token", None):
                self.token_store.write(creds.to_json())
            else:
                logger.warning("google returned no refresh token; keeping the stored one")

            response = RedirectResponse("/#connections", status_code=303)
            self._set_session(response, request, email)
            response.delete_cookie(TX_COOKIE, path="/auth/")
            return response

        @router.post("/auth/logout")
        def logout() -> Any:
            response = JSONResponse({"ok": True})
            response.delete_cookie(SESSION_COOKIE, path="/")
            return response

        return router

    # -- internals ---------------------------------------------------------------

    def _client_config(self) -> Dict[str, Any]:
        return {
            "web": {
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "auth_uri": GOOGLE_AUTH_URI,
                "token_uri": GOOGLE_TOKEN_URI,
            }
        }

    def _redirect_uri(self, request: Request) -> str:
        if self.settings.app_url:
            return f"{self.settings.app_url}/auth/callback"
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        scheme = "https" if _is_https(request) else "http"
        return f"{scheme}://{host}/auth/callback"

    def _google_exchange(
        self, redirect_uri: str, code: str, verifier: str
    ) -> Tuple[Any, Dict[str, Any]]:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import id_token
        from google_auth_oauthlib.flow import Flow

        # Google may grant fewer or more scopes than asked (the user can untick
        # one on the consent screen); oauthlib treats any difference as an
        # error unless told otherwise. The stored token records what was
        # granted, and every API call checks it.
        os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
        flow = Flow.from_client_config(self._client_config(), scopes=LOGIN_SCOPES)
        flow.redirect_uri = redirect_uri
        flow.code_verifier = verifier
        flow.fetch_token(code=code)
        creds = flow.credentials
        claims = id_token.verify_oauth2_token(
            creds.id_token, GoogleRequest(), self.settings.client_id
        )
        return creds, claims


def _is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip() == "https"


_STYLE = (
    "body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f2ea;"
    "color:#1f1c16;font:16px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
    "@media(prefers-color-scheme:dark){body{background:#14130f;color:#efe9dc}}"
    "main{max-width:380px;padding:32px;text-align:center}"
    "h1{font:600 28px/1.1 'Iowan Old Style','New York',Georgia,serif;margin:0 0 10px}"
    "p{color:#8a8372;margin:0 0 22px}"
    "a.btn{display:inline-block;padding:11px 20px;border-radius:9px;background:#c2410c;"
    "color:#fff;text-decoration:none;font-weight:600}"
)

LOGIN_PAGE = (
    "<!doctype html><html lang=en><head><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    f"<title>Semester Sync</title><style>{_STYLE}</style></head><body><main>"
    "<h1>Semester Sync</h1><p>This is a private planner. Sign in with Google to continue.</p>"
    "<a class=btn href=/auth/login>Sign in with Google</a></main></body></html>"
)


def _message_page(title: str, detail: str) -> str:
    from html import escape

    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head><body><main>"
        f"<h1>{escape(title)}</h1><p>{escape(detail)}</p>"
        "<a class=btn href=/>Back</a></main></body></html>"
    )
