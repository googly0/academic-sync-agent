"""Tests for stage 5 — idempotency, backoff, and clean interruption.

A fake Calendar service stands in for Google. It records every call, so these
tests assert on *behaviour* (how many inserts happened, what was retried, what
landed in the checkpoint) rather than on mocks having been called.

Requires the Google client libraries only for the ``HttpError`` type.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

pytest.importorskip("googleapiclient", reason="google-api-python-client not installed")

from googleapiclient.errors import HttpError  # noqa: E402

from academic_sync.calendar_sync.google_calendar import (  # noqa: E402
    DEDUPE_PROPERTY,
    CalendarSyncer,
)
from academic_sync.calendar_sync.state import SyncState  # noqa: E402
from academic_sync.models import AcademicTask, RawExtractedTask  # noqa: E402


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeResponse(dict):
    """Stands in for ``httplib2.Response``.

    Subclasses ``dict`` because the real one does: headers live as lower-cased
    dict *items*, while ``status``/``reason`` are attributes. Getting this
    right matters — a stand-in that stored headers as attributes would let a
    broken Retry-After lookup pass its tests.
    """

    def __init__(self, status: int, retry_after=None):
        super().__init__()
        self.status = status
        self.reason = "fake"
        if retry_after is not None:
            self["retry-after"] = retry_after


def http_error(status: int, reason: str = "", retry_after=None) -> HttpError:
    body = json.dumps(
        {"error": {"code": status, "message": "fake failure", "errors": [{"reason": reason}]}}
    ).encode()
    return HttpError(_FakeResponse(status, retry_after), body)


class FakeEvents:
    def __init__(self, service: "FakeCalendarService"):
        self._service = service

    # -- events().list(...).execute() -------------------------------------
    def list(self, **kwargs):
        self._service.list_calls.append(kwargs)
        prop = kwargs.get("privateExtendedProperty", "")
        key = prop.split("=", 1)[1] if "=" in prop else None
        return _Executable(lambda: self._service._do_list(key))

    # -- events().insert(...).execute() -----------------------------------
    def insert(self, **kwargs):
        self._service.insert_calls.append(kwargs)
        return _Executable(lambda: self._service._do_insert(kwargs["body"]))


class _Executable:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeCalendarService:
    """Minimal stand-in for the Calendar v3 service.

    Args:
        existing: dedupe keys already present on the calendar.
        insert_failures: list of exceptions (or ``None``) to raise on
            successive insert calls, letting a test script a failure sequence.
    """

    def __init__(self, existing=(), insert_failures=()):
        self.existing = {k: {"id": f"evt_existing_{i}"} for i, k in enumerate(existing)}
        self.insert_failures = list(insert_failures)
        self.list_calls = []
        self.insert_calls = []
        self.created = []
        self._counter = 0

    def events(self):
        return FakeEvents(self)

    def _do_list(self, key):
        found = self.existing.get(key)
        return {"items": [found] if found else []}

    def _do_insert(self, body):
        if self.insert_failures:
            failure = self.insert_failures.pop(0)
            if failure is not None:
                raise failure
        self._counter += 1
        event = {"id": f"evt_{self._counter}", "htmlLink": f"https://cal/{self._counter}"}
        self.created.append(body)
        key = body["extendedProperties"]["private"][DEDUPE_PROPERTY]
        self.existing[key] = event
        return event


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_task(name: str, day: int = 13) -> AcademicTask:
    raw = RawExtractedTask(
        course_name="CS 4820",
        task_name=name,
        raw_date_expression="Week 5 Friday",
        grading_weight="10%",
        task_description="desc",
    )
    return AcademicTask.from_raw(raw, exact_due_date=date(2026, 2, day))


@pytest.fixture()
def state(tmp_path):
    return SyncState.load(tmp_path / "sync_state.json")


def make_syncer(service, state, **kwargs):
    kwargs.setdefault("base_delay", 0.0)  # keep tests fast
    kwargs.setdefault("max_delay", 0.0)
    return CalendarSyncer(service, calendar_id="primary", state=state, **kwargs)


# ---------------------------------------------------------------------------
# Happy path + event shape
# ---------------------------------------------------------------------------


class TestCreation:
    def test_creates_one_event_per_task(self, state):
        service = FakeCalendarService()
        report = make_syncer(service, state).sync_tasks([make_task("PS1"), make_task("PS2")])
        assert len(report.created) == 2
        assert len(service.created) == 2

    def test_event_is_all_day_with_exclusive_end(self, state):
        service = FakeCalendarService()
        make_syncer(service, state).sync_tasks([make_task("PS1")])
        body = service.created[0]
        assert body["start"] == {"date": "2026-02-13"}
        # Google's all-day end date is exclusive, so a one-day event ends 02-14.
        assert body["end"] == {"date": "2026-02-14"}

    def test_event_carries_the_dedupe_key(self, state):
        service = FakeCalendarService()
        task = make_task("PS1")
        make_syncer(service, state).sync_tasks([task])
        stored = service.created[0]["extendedProperties"]["private"][DEDUPE_PROPERTY]
        assert stored == task.sync_dedupe_key

    def test_flagged_tasks_are_never_synced(self, state):
        """Defence in depth: even if a flagged task reaches the syncer, it must
        not become an event."""
        flagged = AcademicTask.from_raw(
            RawExtractedTask(course_name="CS 4820", task_name="Final", raw_date_expression="TBD"),
            exact_due_date=None,
        )
        service = FakeCalendarService()
        report = make_syncer(service, state).sync_tasks([flagged])
        assert service.created == []
        assert len(report.skipped_unsyncable) == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_rerun_creates_nothing_new(self, state):
        service = FakeCalendarService()
        tasks = [make_task("PS1"), make_task("PS2")]

        first = make_syncer(service, state).sync_tasks(tasks)
        assert len(first.created) == 2

        second = make_syncer(service, state).sync_tasks(tasks)
        assert second.created == []
        assert len(second.already_synced) == 2
        assert len(service.created) == 2  # nothing new hit the API

    def test_checkpoint_hit_avoids_the_lookup_call(self, state):
        """The local checkpoint is checked first so a no-op re-run costs zero
        API requests."""
        service = FakeCalendarService()
        tasks = [make_task("PS1")]
        make_syncer(service, state).sync_tasks(tasks)
        calls_after_first = len(service.list_calls)

        make_syncer(service, state).sync_tasks(tasks)
        assert len(service.list_calls) == calls_after_first

    def test_lost_state_file_adopts_existing_events(self, tmp_path):
        """The duplicate-catastrophe test: someone deletes sync_state.json.

        The calendar query must find the existing events and adopt them rather
        than creating a second copy of every deadline.
        """
        task = make_task("PS1")
        service = FakeCalendarService(existing=[task.sync_dedupe_key])
        fresh_state = SyncState.load(tmp_path / "gone.json")

        report = make_syncer(service, fresh_state).sync_tasks([task])
        assert report.created == []
        assert len(report.adopted) == 1
        assert service.created == []
        # And the checkpoint is rebuilt, so the next run is free.
        assert fresh_state.is_synced(task.sync_dedupe_key, "primary")


# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------


class TestBackoff:
    @pytest.mark.parametrize(
        "error",
        [
            http_error(429, "rateLimitExceeded"),
            http_error(403, "userRateLimitExceeded"),
            http_error(403, "quotaExceeded"),
            http_error(500),
            http_error(503),
        ],
    )
    def test_retryable_errors_are_retried_then_succeed(self, state, error):
        service = FakeCalendarService(insert_failures=[error, None])
        report = make_syncer(service, state).sync_tasks([make_task("PS1")])
        assert len(report.created) == 1
        assert len(service.insert_calls) == 2  # one failure, one success

    def test_non_retryable_403_stops_immediately(self, state):
        """A permission problem will fail identically forever; retrying it
        just burns quota."""
        service = FakeCalendarService(
            insert_failures=[http_error(403, "insufficientPermissions")] * 5
        )
        report = make_syncer(service, state).sync_tasks([make_task("PS1")])
        assert report.interrupted is True
        assert len(service.insert_calls) == 1
        assert "403" in report.interruption_reason

    def test_404_is_not_retried(self, state):
        service = FakeCalendarService(insert_failures=[http_error(404, "notFound")] * 5)
        report = make_syncer(service, state).sync_tasks([make_task("PS1")])
        assert report.interrupted is True
        assert len(service.insert_calls) == 1

    def test_exhausted_retries_stop_the_run(self, state):
        service = FakeCalendarService(
            insert_failures=[http_error(429, "rateLimitExceeded")] * 10
        )
        report = make_syncer(service, state, max_attempts=3).sync_tasks([make_task("PS1")])
        assert report.interrupted is True
        assert len(service.insert_calls) == 3
        assert "gave up after 3 attempts" in report.interruption_reason

    def test_retry_after_header_is_honoured(self, state):
        service = FakeCalendarService(
            insert_failures=[http_error(429, "rateLimitExceeded", retry_after="0"), None]
        )
        report = make_syncer(service, state).sync_tasks([make_task("PS1")])
        assert len(report.created) == 1


# ---------------------------------------------------------------------------
# Interruption and resume — the headline fault-tolerance requirement
# ---------------------------------------------------------------------------


class TestResume:
    def test_midbatch_failure_persists_prior_progress(self, tmp_path):
        """Two succeed, the third exhausts retries. The run must stop cleanly
        with the first two durably checkpointed."""
        state_path = tmp_path / "sync_state.json"
        state = SyncState.load(state_path)
        tasks = [make_task(f"PS{i}", day=13) for i in range(1, 6)]
        # Tasks differ by name, so each has a distinct dedupe key.
        service = FakeCalendarService(
            insert_failures=[None, None] + [http_error(429, "rateLimitExceeded")] * 20
        )

        report = make_syncer(service, state, max_attempts=2).sync_tasks(tasks)

        assert report.interrupted is True
        assert len(report.created) == 2
        assert len(report.remaining) == 3

        # Durable on disk, not just in memory.
        reloaded = SyncState.load(state_path)
        assert reloaded.synced_count("primary") == 2

    def test_rerun_after_interruption_completes_the_remainder(self, tmp_path):
        state_path = tmp_path / "sync_state.json"
        tasks = [make_task(f"PS{i}") for i in range(1, 6)]

        # Run 1: two succeed, then the API starts failing.
        service1 = FakeCalendarService(
            insert_failures=[None, None] + [http_error(429, "rateLimitExceeded")] * 20
        )
        first = make_syncer(service1, SyncState.load(state_path), max_attempts=2).sync_tasks(tasks)
        assert first.interrupted is True

        # Run 2: API healthy again. Only the remaining three are created.
        service2 = FakeCalendarService()
        second = make_syncer(service2, SyncState.load(state_path)).sync_tasks(tasks)

        assert second.interrupted is False
        assert len(second.created) == 3
        assert len(second.already_synced) == 2
        assert len(service2.created) == 3  # no duplicates of the first two
        assert SyncState.load(state_path).synced_count("primary") == 5

    def test_report_summary_is_human_readable(self, state):
        service = FakeCalendarService()
        report = make_syncer(service, state).sync_tasks([make_task("PS1")])
        assert "created=1" in report.summary()
