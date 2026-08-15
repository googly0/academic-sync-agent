"""Tests for the checkpoint file — the mechanism behind resume-after-failure.

No Google dependencies are needed here, which is itself part of the design:
the resume logic can be verified without credentials or a network.
"""

from __future__ import annotations

import json

import pytest

from academic_sync.calendar_sync.state import STATE_VERSION, StateFileError, SyncState


@pytest.fixture()
def state_path(tmp_path):
    return tmp_path / "sync_state.json"


class TestRoundTrip:
    def test_missing_file_starts_empty(self, state_path):
        """First run is not an error."""
        state = SyncState.load(state_path)
        assert state.synced_count() == 0

    def test_save_then_load_preserves_records(self, state_path):
        state = SyncState.load(state_path)
        state.mark_synced(
            "key-a",
            "primary",
            event_id="evt_1",
            course_name="CS 4820",
            task_name="PS3",
            due_date="2026-02-13",
        )
        state.save()

        reloaded = SyncState.load(state_path)
        assert reloaded.is_synced("key-a", "primary")
        record = reloaded.get("key-a", "primary")
        assert record.event_id == "evt_1"
        assert record.task_name == "PS3"
        assert record.due_date == "2026-02-13"

    def test_calendars_are_isolated(self, state_path):
        """Syncing the same syllabus to a second calendar must not be treated
        as already done."""
        state = SyncState.load(state_path)
        state.mark_synced("key-a", "primary", event_id="evt_1")
        state.save()

        reloaded = SyncState.load(state_path)
        assert reloaded.is_synced("key-a", "primary") is True
        assert reloaded.is_synced("key-a", "team@group.calendar.google.com") is False

    def test_forget_allows_recreation(self, state_path):
        state = SyncState.load(state_path)
        state.mark_synced("key-a", "primary", event_id="evt_1")
        state.forget("key-a", "primary")
        assert state.is_synced("key-a", "primary") is False


class TestResumeSemantics:
    def test_partial_progress_survives_an_abrupt_stop(self, state_path):
        """Simulates the real failure: three of five events created, then the
        process dies. A fresh load must know about exactly those three."""
        state = SyncState.load(state_path)
        for i in range(3):
            state.mark_synced(f"key-{i}", "primary", event_id=f"evt_{i}")
            state.save()  # per-event save, exactly as the syncer does

        resumed = SyncState.load(state_path)
        remaining = [k for k in (f"key-{i}" for i in range(5))
                     if not resumed.is_synced(k, "primary")]
        assert remaining == ["key-3", "key-4"]

    def test_synced_count_per_calendar_and_total(self, state_path):
        state = SyncState.load(state_path)
        state.mark_synced("a", "primary", event_id="1")
        state.mark_synced("b", "primary", event_id="2")
        state.mark_synced("c", "other", event_id="3")
        assert state.synced_count("primary") == 2
        assert state.synced_count("other") == 1
        assert state.synced_count() == 3


class TestCorruptionHandling:
    def test_unreadable_file_raises_instead_of_starting_fresh(self, state_path):
        """Starting fresh from a corrupt checkpoint would duplicate every
        existing event, so it must be a loud failure."""
        state_path.write_text("{ this is not json", encoding="utf-8")
        with pytest.raises(StateFileError) as exc:
            SyncState.load(state_path)
        assert "unreadable" in str(exc.value)

    def test_unknown_version_raises(self, state_path):
        state_path.write_text(
            json.dumps({"version": STATE_VERSION + 99, "calendars": {}}), encoding="utf-8"
        )
        with pytest.raises(StateFileError):
            SyncState.load(state_path)


class TestAtomicWrite:
    def test_no_temp_files_left_behind(self, state_path):
        state = SyncState.load(state_path)
        state.mark_synced("key-a", "primary", event_id="evt_1")
        state.save()
        state.save()
        leftovers = [p.name for p in state_path.parent.iterdir() if p.name != state_path.name]
        assert leftovers == []

    def test_written_file_is_valid_json_with_a_version(self, state_path):
        state = SyncState.load(state_path)
        state.mark_synced("key-a", "primary", event_id="evt_1")
        state.save()
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        assert payload["version"] == STATE_VERSION
        assert payload["calendars"]["primary"]["key-a"]["event_id"] == "evt_1"

    def test_parent_directory_is_created(self, tmp_path):
        nested = tmp_path / "deep" / "nested" / "sync_state.json"
        state = SyncState.load(nested)
        state.mark_synced("k", "primary", event_id="e")
        state.save()
        assert nested.exists()
