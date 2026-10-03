"""Tests for the web app's persistent store."""

from __future__ import annotations

from datetime import date

import pytest

from academic_sync.models import AcademicTask
from academic_sync.store import (
    STATUS_ACTIVE,
    STATUS_DISMISSED,
    STATUS_REVIEW,
    Store,
    task_identity,
)


@pytest.fixture(scope="session")
def _pg_server(tmp_path_factory):
    pgserver = pytest.importorskip("pgserver", reason="pgserver not installed")
    server = pgserver.get_server(tmp_path_factory.mktemp("pg"))
    yield server
    server.cleanup()


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    """Every test here runs against both databases the app supports."""
    if request.param == "sqlite":
        s = Store(tmp_path / "app.db")
    else:
        server = request.getfixturevalue("_pg_server")
        s = Store.from_url(server.get_uri())
        # One server for the session; start each test from empty tables.
        from academic_sync.store import metadata

        with s._engine.begin() as conn:
            for table in reversed(metadata.sorted_tables):
                conn.execute(table.delete())
    yield s
    s.close()


def good(name="Problem Set 1", day=13):
    return AcademicTask(course_name="CS 101", task_name=name, exact_due_date=date(2026, 2, day))


def flagged(name="Final Project", phrase="TBD"):
    return AcademicTask(course_name="CS 101", task_name=name, raw_date_expression=phrase)


class TestImportIdempotency:
    def test_reimporting_the_same_tasks_adds_nothing(self, store):
        source = store.add_source("pdf", "syllabus.pdf")
        assert store.add_tasks([good(), flagged()], source_id=source) == {"added": 2, "duplicates": 0}
        again = store.add_source("pdf", "syllabus.pdf")
        assert store.add_tasks([good(), flagged()], source_id=again) == {"added": 0, "duplicates": 2}
        assert len(store.tasks()) == 2

    def test_identity_ignores_case_and_whitespace(self):
        a = flagged(name="Final  Project", phrase="TBD")
        b = flagged(name="final project", phrase=" tbd ")
        assert task_identity(a) == task_identity(b)

    def test_dismissed_task_is_not_revived_by_reimport(self, store):
        store.add_tasks([flagged()], source_id=None)
        task_id = store.tasks()[0].id
        store.dismiss_task(task_id)
        store.add_tasks([flagged()], source_id=None)
        assert store.tasks() == []
        assert store.tasks(include_dismissed=True)[0].status == STATUS_DISMISSED


class TestStatusFollowsTheGate:
    def test_status_is_computed_from_the_review_gate(self, store):
        store.add_tasks([good(), flagged()], source_id=None)
        statuses = {t.task.task_name: t.status for t in store.tasks()}
        assert statuses == {"Problem Set 1": STATUS_ACTIVE, "Final Project": STATUS_REVIEW}

    def test_replacing_a_task_recomputes_status(self, store):
        store.add_tasks([flagged()], source_id=None)
        stored = store.tasks()[0]
        fixed = stored.task.model_copy(update={"exact_due_date": date(2026, 4, 30)})
        # model_copy skips validation; rebuild so the gate runs, as Workspace does.
        fixed = AcademicTask(**fixed.model_dump(exclude={"sync_dedupe_key"}))
        assert store.replace_task(stored.id, fixed).status == STATUS_ACTIVE

    def test_round_trip_preserves_the_task(self, store):
        task = AcademicTask(
            course_name="CS 101",
            task_name="Exams",
            exact_due_date=date(2026, 5, 4),
            end_date=date(2026, 5, 8),
            contradiction_quotes=["a", "b"],
        )
        store.add_tasks([task], source_id=None)
        loaded = store.tasks()[0].task
        assert loaded.end_date == date(2026, 5, 8)
        assert loaded.sync_dedupe_key == task.sync_dedupe_key


class TestSyncRecords:
    def test_error_never_erases_an_earlier_success(self, store):
        store.add_tasks([good()], source_id=None)
        task_id = store.tasks()[0].id
        store.record_sync(task_id, "gcal", dedupe_key="primary:k", remote_id="evt1")
        store.record_sync(task_id, "gcal", dedupe_key="primary:k", error="HTTP 500")
        record = store.syncs_for()[task_id]["gcal"]
        assert record["remote_id"] == "evt1" and record["synced_at"]
        assert record["last_error"] == "HTTP 500"

    def test_success_clears_the_error(self, store):
        store.add_tasks([good()], source_id=None)
        task_id = store.tasks()[0].id
        store.record_sync(task_id, "notion", dedupe_key="db:k", error="boom")
        store.record_sync(task_id, "notion", dedupe_key="db:k", remote_id="page")
        assert store.syncs_for()[task_id]["notion"]["last_error"] is None


def test_settings_round_trip_and_delete(store):
    store.set_settings({"semester_start": "2026-01-12", "notion_token": "x"})
    assert store.get_setting("semester_start") == "2026-01-12"
    store.set_settings({"notion_token": None})
    assert store.get_setting("notion_token") is None


def test_external_source_ids_are_tracked(store):
    store.add_source("gmail", "PS3 due", external_id="msg-1")
    assert store.has_source("gmail", "msg-1")
    assert not store.has_source("gmail", "msg-2")


class TestJobs:
    def test_progress_and_result_round_trip(self, store):
        store.create_job("j1", "import", "syllabus.pdf")
        store.update_job("j1", status="running")
        store.set_job_stage("j1", "extract_text", "done", "2 page(s)")
        store.set_job_stage("j1", "extract_facts", "running", "querying stub")
        store.update_job("j1", status="done", result={"added": 3})
        job = store.get_job("j1")
        assert job["status"] == "done" and job["result"] == {"added": 3}
        assert job["stages"]["extract_text"] == {"state": "done", "message": "2 page(s)"}
        assert job["stages"]["extract_facts"]["state"] == "running"

    def test_unknown_job_is_none(self, store):
        assert store.get_job("nope") is None

    def test_only_recent_jobs_are_kept(self, store):
        for n in range(45):
            store.create_job(f"j{n:02d}", "import", "x")
        assert store.get_job("j00") is None
        assert store.get_job("j44") is not None


class TestSecretsAndCheckpoint:
    def test_secret_round_trip_and_delete(self, store):
        store.put_secret("google", "ciphertext-1")
        store.put_secret("google", "ciphertext-2")
        assert store.get_secret("google") == "ciphertext-2"
        store.delete_secret("google")
        assert store.get_secret("google") is None

    def test_checkpoint_is_scoped_by_calendar(self, store):
        kw = dict(synced_at="t", course_name="C", task_name="T", due_date="2026-02-13")
        store.checkpoint_put("primary", "k1", event_id="e1", **kw)
        store.checkpoint_put("primary", "k1", event_id="e2", **kw)  # upsert
        store.checkpoint_put("other", "k1", event_id="e3", **kw)
        assert [r["event_id"] for r in store.checkpoint_rows("primary")] == ["e2"]
        store.checkpoint_delete("primary", "k1")
        assert store.checkpoint_rows("primary") == []
        assert len(store.checkpoint_rows("other")) == 1


def test_database_url_picks_the_psycopg3_driver():
    from academic_sync.store import database_url

    assert database_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert database_url("postgresql://u:p@h/db?sslmode=require") == (
        "postgresql+psycopg://u:p@h/db?sslmode=require"
    )
    assert database_url("sqlite:///x.db") == "sqlite:///x.db"
