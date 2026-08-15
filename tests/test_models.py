"""Tests for stage 4 — the validation gate.

Focus: the three review branches, and the guarantee that a flagged task can
never reach the calendar.
"""

from __future__ import annotations

from datetime import date

from academic_sync.models import (
    REVIEW_CONTRADICTION,
    REVIEW_MISSING_FIELDS,
    REVIEW_UNRESOLVED_DATE,
    AcademicTask,
    RawExtractedTask,
)


def _complete_raw(**overrides) -> RawExtractedTask:
    data = dict(
        course_name="CS 4820",
        task_name="Problem Set 3",
        raw_date_expression="Week 5 Friday",
        grading_weight="10%",
        task_description="Prove the greedy algorithm is optimal.",
    )
    data.update(overrides)
    return RawExtractedTask(**data)


class TestHappyPath:
    def test_complete_task_is_syncable(self):
        task = AcademicTask.from_raw(_complete_raw(), exact_due_date=date(2026, 2, 13))
        assert task.requires_manual_review is False
        assert task.review_reason is None
        assert task.is_syncable is True
        assert task.sync_dedupe_key is not None

    def test_serialises_the_due_date_as_iso_8601(self):
        task = AcademicTask.from_raw(_complete_raw(), exact_due_date=date(2026, 2, 13))
        assert task.model_dump(mode="json")["exact_due_date"] == "2026-02-13"


class TestReviewBranches:
    def test_missing_required_field(self):
        task = AcademicTask.from_raw(
            _complete_raw(grading_weight=None), exact_due_date=date(2026, 2, 13)
        )
        assert task.requires_manual_review is True
        assert REVIEW_MISSING_FIELDS in task.review_reason
        assert "grading_weight" in task.review_reason
        assert task.is_syncable is False

    def test_blank_string_counts_as_missing(self):
        task = AcademicTask.from_raw(
            _complete_raw(task_description="   "), exact_due_date=date(2026, 2, 13)
        )
        assert REVIEW_MISSING_FIELDS in task.review_reason

    def test_contradiction(self):
        raw = _complete_raw(
            contradiction_detected=True,
            contradiction_quotes=["due Oct 10", "schedule table: Oct 17"],
        )
        task = AcademicTask.from_raw(raw, exact_due_date=date(2026, 10, 10))
        assert task.requires_manual_review is True
        assert REVIEW_CONTRADICTION in task.review_reason
        # Both quotes must survive into the review file.
        assert "Oct 10" in task.review_reason and "Oct 17" in task.review_reason
        assert task.is_syncable is False

    def test_unresolvable_date(self):
        task = AcademicTask.from_raw(
            _complete_raw(raw_date_expression="TBD"),
            exact_due_date=None,
            date_resolution_error="expression is a placeholder",
        )
        assert task.requires_manual_review is True
        assert REVIEW_UNRESOLVED_DATE in task.review_reason
        assert "TBD" in task.review_reason
        assert "placeholder" in task.review_reason
        assert task.is_syncable is False

    def test_all_three_reasons_are_reported_together(self):
        raw = _complete_raw(
            grading_weight=None,
            raw_date_expression="TBD",
            contradiction_detected=True,
            contradiction_quotes=["Oct 10", "Oct 17"],
        )
        task = AcademicTask.from_raw(raw, exact_due_date=None)
        for code in (REVIEW_MISSING_FIELDS, REVIEW_CONTRADICTION, REVIEW_UNRESOLVED_DATE):
            assert code in task.review_reason

    def test_flags_cannot_be_forged(self):
        """Constructing with requires_manual_review=False must not bypass the
        gate — the validator recomputes from the underlying data."""
        task = AcademicTask(
            course_name="CS 4820",
            task_name="PS3",
            exact_due_date=None,
            grading_weight="10%",
            task_description="x",
            requires_manual_review=False,
            review_reason=None,
        )
        assert task.requires_manual_review is True
        assert task.is_syncable is False


class TestDedupeKey:
    def test_key_is_stable_across_constructions(self):
        a = AcademicTask.from_raw(_complete_raw(), exact_due_date=date(2026, 2, 13))
        b = AcademicTask.from_raw(_complete_raw(), exact_due_date=date(2026, 2, 13))
        assert a.sync_dedupe_key == b.sync_dedupe_key

    def test_key_ignores_case_and_whitespace_noise(self):
        a = AcademicTask.from_raw(_complete_raw(), exact_due_date=date(2026, 2, 13))
        b = AcademicTask.from_raw(
            _complete_raw(course_name="cs 4820  ", task_name="  Problem  Set 3"),
            exact_due_date=date(2026, 2, 13),
        )
        assert a.sync_dedupe_key == b.sync_dedupe_key

    def test_key_changes_with_the_date(self):
        """A rescheduled deadline is a different event, not the same one."""
        a = AcademicTask.from_raw(_complete_raw(), exact_due_date=date(2026, 2, 13))
        b = AcademicTask.from_raw(_complete_raw(), exact_due_date=date(2026, 2, 20))
        assert a.sync_dedupe_key != b.sync_dedupe_key

    def test_key_changes_with_course_and_task(self):
        base = AcademicTask.from_raw(_complete_raw(), exact_due_date=date(2026, 2, 13))
        other_course = AcademicTask.from_raw(
            _complete_raw(course_name="CS 2110"), exact_due_date=date(2026, 2, 13)
        )
        other_task = AcademicTask.from_raw(
            _complete_raw(task_name="Problem Set 4"), exact_due_date=date(2026, 2, 13)
        )
        assert len({base.sync_dedupe_key, other_course.sync_dedupe_key, other_task.sync_dedupe_key}) == 3

    def test_no_key_without_a_resolved_date(self):
        """No date means no calendar identity — callers must treat None as
        'not syncable' rather than keying on it."""
        task = AcademicTask.from_raw(
            _complete_raw(raw_date_expression="TBD"), exact_due_date=None
        )
        assert task.sync_dedupe_key is None
