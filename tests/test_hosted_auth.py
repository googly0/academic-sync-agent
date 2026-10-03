"""The deployed app's sign-in, sessions and request token. Fully offline:
Google's code exchange is replaced by a fake that returns chosen claims."""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from academic_sync.calendar_sync.auth import CALENDAR_SCOPE, DbTokenStore  # noqa: E402
from academic_sync.secrets_box import SecretsBox  # noqa: E402
from academic_sync.store import Store  # noqa: E402
from academic_sync.web.app import create_app  # noqa: E402
from academic_sync.web.auth import (  # noqa: E402
    HostedAuth,
    HostedConfigError,
    HostedSettings,
    csrf_token,
)
from academic_sync.web.hosted_app import build_hosted_app, load  # noqa: E402
from academic_sync.workspace import Workspace  # noqa: E402

SECRET = "x" * 48
ME = "me@example.com"


class FakeCreds:
    refresh_token = "refresh-secret"

    def to_json(self):
        return json.dumps(
            {
                "token": "a", "refresh_token": "refresh-secret", "client_id": "c",
                "client_secret": "s", "token_uri": "https://oauth2.googleapis.com/token",
                "scopes": [CALENDAR_SCOPE],
            }
        )


class Harness:
    def __init__(self, tmp_path):
        self.store = Store(tmp_path / "h.db")
        self.box = SecretsBox(Fernet.generate_key().decode())
        self.tokens = DbTokenStore(self.store, self.box)
        self.claims = {"email": ME, "email_verified": True}
        self.settings = HostedSettings(ME, SECRET, "cid", "csecret")
        auth = HostedAuth(self.settings, self.tokens, exchange=lambda *a: (FakeCreds(), self.claims))
        workspace = Workspace(self.store, hosted=True, token_store=self.tokens, box=self.box)
        self.app = create_app(workspace=workspace, hosted=auth)
        self.auth = auth

    def client(self):
        return TestClient(self.app, follow_redirects=False)

    def sign_in(self, client):
        login = client.get("/auth/login")
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        return client.get(f"/auth/callback?code=abc&state={state}")


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


class TestAnonymous:
    def test_home_shows_a_sign_in_page_not_the_app(self, h):
        r = h.client().get("/")
        assert r.status_code == 200 and "Sign in with Google" in r.text
        assert "app-token" not in r.text

    def test_api_requires_sign_in(self, h):
        c = h.client()
        assert c.get("/api/state").status_code == 401
        assert c.post("/api/settings", json={}).status_code == 401
        assert c.get("/api/jobs/abcdefgh").status_code == 401
        assert c.get("/api/demo-pdf").status_code == 401

    def test_static_files_are_public(self, h):
        assert h.client().get("/static/app.css").status_code == 200

    def test_login_redirects_to_google_asking_for_all_three_scopes(self, h):
        r = h.client().get("/auth/login")
        url = urlparse(r.headers["location"])
        q = parse_qs(url.query)
        assert url.netloc == "accounts.google.com"
        scopes = q["scope"][0]
        assert "openid" in scopes and "calendar.events" in scopes and "gmail.readonly" in scopes
        assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
        assert q["code_challenge_method"] == ["S256"]  # PKCE
        assert q["login_hint"] == [ME]


class TestCallback:
    def test_allowed_email_gets_a_session_and_the_token_is_stored_encrypted(self, h):
        c = h.client()
        r = h.sign_in(c)
        assert r.status_code == 303
        assert "HttpOnly" in r.headers["set-cookie"] and "samesite=lax" in r.headers["set-cookie"].lower()
        assert c.get("/api/state").json()["hosted"]["email"] == ME
        raw = h.store.get_secret("google_token")
        assert raw and "refresh-secret" not in raw
        assert json.loads(h.tokens.read())["refresh_token"] == "refresh-secret"

    def test_other_google_accounts_are_refused_and_nothing_is_stored(self, h):
        h.claims = {"email": "stranger@example.com", "email_verified": True}
        c = h.client()
        r = h.sign_in(c)
        assert r.status_code == 403
        assert "as_session" not in r.headers.get("set-cookie", "")
        assert h.store.get_secret("google_token") is None
        assert c.get("/api/state").status_code == 401

    def test_unverified_email_is_refused_even_if_it_matches(self, h):
        h.claims = {"email": ME, "email_verified": False}
        assert h.sign_in(h.client()).status_code == 403

    def test_email_comparison_ignores_case(self, h):
        h.claims = {"email": ME.upper(), "email_verified": True}
        assert h.sign_in(h.client()).status_code == 303

    def test_wrong_state_is_rejected(self, h):
        c = h.client()
        c.get("/auth/login")
        assert c.get("/auth/callback?code=abc&state=forged").status_code == 400

    def test_callback_without_starting_sign_in_is_rejected(self, h):
        assert h.client().get("/auth/callback?code=abc&state=x").status_code == 400

    def test_google_error_is_shown_not_raised(self, h):
        assert h.client().get("/auth/callback?error=access_denied").status_code == 400

    def test_a_failed_exchange_is_a_friendly_400(self, h):
        def boom(*a):
            raise RuntimeError("invalid_grant")

        h.auth._exchange = boom
        assert h.sign_in(h.client()).status_code == 400


class TestSessions:
    def test_forged_cookie_is_ignored(self, h):
        c = h.client()
        c.cookies.set("as_session", "not-a-signed-value")
        assert c.get("/api/state").status_code == 401

    def test_session_signed_with_another_secret_is_ignored(self, h):
        from itsdangerous import URLSafeTimedSerializer

        forged = URLSafeTimedSerializer("y" * 48, salt="session").dumps({"email": ME, "sid": "s"})
        c = h.client()
        c.cookies.set("as_session", forged)
        assert c.get("/api/state").status_code == 401

    def test_removing_the_email_from_the_allowlist_ends_existing_sessions(self, h):
        c = h.client()
        h.sign_in(c)
        assert c.get("/api/state").status_code == 200
        h.auth.settings = HostedSettings("someone-else@example.com", SECRET, "cid", "csecret")
        assert c.get("/api/state").status_code == 401

    def test_logout_ends_the_session(self, h):
        c = h.client()
        h.sign_in(c)
        token = _page_token(c)
        assert c.post("/auth/logout", headers={"X-App-Token": token}).status_code == 200
        assert c.get("/api/state").status_code == 401


class TestRequestToken:
    def test_page_carries_a_token_derived_from_the_session(self, h):
        c = h.client()
        h.sign_in(c)
        assert _page_token(c)

    def test_mutations_need_the_token(self, h):
        c = h.client()
        h.sign_in(c)
        assert c.post("/api/settings", json={"semester_start": "2026-01-12"}).status_code == 403
        assert c.post(
            "/api/settings", json={"semester_start": "2026-01-12"},
            headers={"X-App-Token": "wrong"},
        ).status_code == 403
        assert c.post(
            "/api/settings", json={"semester_start": "2026-01-12"},
            headers={"X-App-Token": _page_token(c)},
        ).status_code == 200

    def test_another_sessions_token_does_not_work(self, h):
        a, b = h.client(), h.client()
        h.sign_in(a)
        h.sign_in(b)
        assert _page_token(a) != _page_token(b)
        r = a.post("/api/settings", json={"week_start": "0"}, headers={"X-App-Token": _page_token(b)})
        assert r.status_code == 403

    def test_token_is_stable_so_any_instance_can_verify_it(self):
        assert csrf_token(SECRET, "sid1") == csrf_token(SECRET, "sid1")
        assert csrf_token(SECRET, "sid1") != csrf_token(SECRET, "sid2")
        assert csrf_token(SECRET, "sid1") != csrf_token("z" * 48, "sid1")


class TestHostedJobs:
    def test_work_runs_inside_the_request_with_the_browsers_job_id(self, h):
        c = h.client()
        h.sign_in(c)
        headers = {"X-App-Token": _page_token(c)}
        c.post("/api/settings", json={"semester_start": "2026-01-12", "llm_backend": "stub"}, headers=headers)
        r = c.post(
            "/api/import/gmail", json={"job_id": "abcd1234ef"}, headers=headers
        )
        assert r.json() == {"job_id": "abcd1234ef"}
        job = c.get("/api/jobs/abcd1234ef").json()
        # Finished before the response was sent — the property that matters
        # on a host that freezes the function afterwards. (It errors only
        # because no Gmail is connected in this test.)
        assert job["status"] == "error" and "Google account" in job["error"]

    def test_job_ids_are_validated_and_not_reusable(self, h):
        c = h.client()
        h.sign_in(c)
        headers = {"X-App-Token": _page_token(c)}
        assert c.post("/api/sync", json={"targets": ["gcal"], "job_id": "../x"}, headers=headers).status_code == 400
        ok = c.post("/api/sync", json={"targets": ["gcal"], "job_id": "samejob123"}, headers=headers)
        assert ok.status_code == 200
        again = c.post("/api/sync", json={"targets": ["gcal"], "job_id": "samejob123"}, headers=headers)
        assert again.status_code == 409

    def test_connect_google_redirects_to_the_web_flow(self, h):
        c = h.client()
        h.sign_in(c)
        r = c.post("/api/connections/google", json={}, headers={"X-App-Token": _page_token(c)})
        assert r.json() == {"redirect": "/auth/login"}


class TestConfiguration:
    ENV = {
        "ALLOWED_EMAIL": ME, "SESSION_SECRET": SECRET, "GOOGLE_CLIENT_ID": "c",
        "GOOGLE_CLIENT_SECRET": "s", "DATABASE_URL": "sqlite:///:memory:",
        "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }

    def test_missing_variables_are_all_named(self):
        with pytest.raises(HostedConfigError) as info:
            HostedSettings.from_env({})
        for name in ("ALLOWED_EMAIL", "SESSION_SECRET", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
            assert name in str(info.value)

    def test_short_session_secret_is_rejected(self):
        with pytest.raises(HostedConfigError, match="too short"):
            HostedSettings.from_env({**self.ENV, "SESSION_SECRET": "short"})

    def test_missing_database_is_explained(self):
        env = {k: v for k, v in self.ENV.items() if k != "DATABASE_URL"}
        with pytest.raises(HostedConfigError, match="Neon"):
            build_hosted_app(env)

    def test_complete_configuration_builds_a_working_app(self):
        app = build_hosted_app(self.ENV)
        assert TestClient(app).get("/").status_code == 200

    def test_incomplete_configuration_serves_a_helpful_503(self, monkeypatch):
        for key in self.ENV:
            monkeypatch.delenv(key, raising=False)
        r = TestClient(load()).get("/anything")
        assert r.status_code == 503 and "ALLOWED_EMAIL" in r.text


def _page_token(client):
    html = client.get("/").text
    marker = 'name="app-token" content="'
    return html.split(marker, 1)[1].split('"', 1)[0]
