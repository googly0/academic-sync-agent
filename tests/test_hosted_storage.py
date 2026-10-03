"""Hosted-mode storage: encrypted secrets, DB token store, DB checkpoint."""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from academic_sync.calendar_sync.auth import (
    CALENDAR_SCOPE,
    GMAIL_SCOPE,
    DbTokenStore,
    FileTokenStore,
    google_status,
    load_google_credentials,
)
from academic_sync.calendar_sync.errors import CalendarAuthError
from academic_sync.calendar_sync.state import DbSyncState
from academic_sync.secrets_box import SecretsBox, SecretsError
from academic_sync.store import Store

TOKEN = {
    "token": "access",
    "refresh_token": "refresh",
    "client_id": "cid",
    "client_secret": "csecret",
    "token_uri": "https://oauth2.googleapis.com/token",
    "scopes": [CALENDAR_SCOPE],
}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "app.db")
    yield s
    s.close()


@pytest.fixture
def box():
    return SecretsBox(Fernet.generate_key().decode())


class TestSecretsBox:
    def test_round_trip(self, box):
        assert box.decrypt(box.encrypt("hunter2")) == "hunter2"

    def test_ciphertext_does_not_contain_the_secret(self, box):
        assert "hunter2" not in box.encrypt("hunter2")

    def test_wrong_key_fails_closed(self, box):
        other = SecretsBox(Fernet.generate_key().decode())
        with pytest.raises(SecretsError, match="could not be decrypted"):
            other.decrypt(box.encrypt("hunter2"))

    def test_tampered_value_fails_closed(self, box):
        c = box.encrypt("hunter2")
        flipped = c[:-4] + ("AAAA" if not c.endswith("AAAA") else "BBBB")
        with pytest.raises(SecretsError):
            box.decrypt(flipped)

    def test_missing_key_explains_how_to_make_one(self, monkeypatch):
        monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
        with pytest.raises(SecretsError, match="Fernet.generate_key"):
            SecretsBox()

    def test_malformed_key_is_rejected(self):
        with pytest.raises(SecretsError, match="not a valid Fernet key"):
            SecretsBox("not-a-key")


class TestDbTokenStore:
    def test_token_is_stored_encrypted(self, store, box):
        DbTokenStore(store, box).write(json.dumps(TOKEN))
        raw = store.get_secret("google_token")
        assert "refresh" not in raw
        assert json.loads(DbTokenStore(store, box).read()) == TOKEN

    def test_status_reads_scopes_from_the_database(self, store, box):
        tokens = DbTokenStore(store, box)
        assert google_status(token_store=tokens)["connected"] is False
        tokens.write(json.dumps(TOKEN))
        status = google_status(token_store=tokens)
        assert status["connected"] and status["calendar"] and not status["gmail"]

    def test_status_reports_an_undecryptable_token_instead_of_raising(self, store, box):
        DbTokenStore(store, box).write(json.dumps(TOKEN))
        wrong = DbTokenStore(store, SecretsBox(Fernet.generate_key().decode()))
        assert "could not be decrypted" in google_status(token_store=wrong)["error"]

    def test_missing_scope_needs_consent_and_names_the_database(self, store, box):
        tokens = DbTokenStore(store, box)
        tokens.write(json.dumps(TOKEN))
        with pytest.raises(CalendarAuthError, match="the app database"):
            load_google_credentials([GMAIL_SCOPE], token_store=tokens, allow_interactive=False)

    def test_file_store_matches_the_old_behaviour(self, tmp_path):
        tokens = FileTokenStore(tmp_path / "token.json")
        assert tokens.read() is None
        tokens.write(json.dumps(TOKEN))
        assert oct((tmp_path / "token.json").stat().st_mode)[-3:] == "600"


class TestDbSyncState:
    def test_writes_through_and_survives_a_new_instance(self, store):
        DbSyncState(store).mark_synced(
            "k1", "primary", event_id="e1", course_name="C", task_name="T", due_date="2026-02-13"
        )
        again = DbSyncState(store)
        assert again.is_synced("k1", "primary")
        assert again.get("k1", "primary").event_id == "e1"
        assert not again.is_synced("k1", "other-calendar")

    def test_forget(self, store):
        state = DbSyncState(store)
        state.mark_synced("k1", "primary", event_id="e1")
        state.forget("k1", "primary")
        assert not state.is_synced("k1", "primary")
