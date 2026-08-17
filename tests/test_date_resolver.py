"""Tests for stage 3 — the module most likely to be silently wrong.

Every calendar fact asserted here is stated explicitly in a comment, because a
test that merely agrees with the implementation proves nothing. Reference
anchors used throughout:

    2026-01-12 is a Monday
    2026-02-13 is a Friday   (Week 5 Friday, semester starting 2026-01-12)
    2026-10-01 is a Thursday -> first Tuesday of Oct 2026 is the 6th,
                                so the second Tuesday is 2026-10-13
    2026-10-10 is a Saturday (used for the weekday-mismatch tests)
    2026-11-27 is the last Friday of November 2026
"""

from __future__ import annotations

from datetime import date

import pytest

from academic_sync.resolution import (
    AmbiguousDateError,
    DateOutOfRangeError,
    DateResolver,
    UnresolvableDateError,
    resolve_date,
)

#: Spring 2026 semester starting on a Monday.
SEMESTER_START = date(2026, 1, 12)


@pytest.fixture()
def resolver() -> DateResolver:
    return DateResolver(SEMESTER_START)


# ---------------------------------------------------------------------------
# Sanity: the calendar facts the rest of the file relies on
# ---------------------------------------------------------------------------


def test_reference_calendar_facts_hold():
    """If these fail, the expectations below are meaningless."""
    assert SEMESTER_START.weekday() == 0  # Monday
    assert date(2026, 2, 13).weekday() == 4  # Friday
    assert date(2026, 10, 1).weekday() == 3  # Thursday
    assert date(2026, 10, 10).weekday() == 5  # Saturday
    assert date(2026, 10, 13).weekday() == 1  # Tuesday
    assert date(2026, 11, 27).weekday() == 4  # Friday


# ---------------------------------------------------------------------------
# "Week N [Weekday]"
# ---------------------------------------------------------------------------


class TestWeekPatterns:
    def test_week_5_friday(self, resolver):
        # Week 1 is Jan 12-18; Week 5 starts Jan 12 + 28 = Feb 9 (Mon);
        # Friday of that week is Feb 13.
        assert resolver.resolve("Week 5 Friday") == date(2026, 2, 13)

    def test_week_1_monday_is_the_semester_start(self, resolver):
        assert resolver.resolve("Week 1 Monday") == SEMESTER_START

    @pytest.mark.parametrize(
        "expression",
        [
            "Week 5 Friday",
            "week 5 friday",
            "WEEK 5 FRIDAY",
            "Week 5, Friday",
            "Week 5: Friday",
            "Week 5 (Friday)",
            "Wk 5 Fri",
            "week #5 friday",
            "Friday of Week 5",
            "due Week 5 Friday",
            "due by 11:59pm on Week 5 Friday",
        ],
    )
    def test_week_5_friday_spelling_variants(self, resolver, expression):
        """Real syllabi write this a dozen ways; all must land on one date."""
        assert resolver.resolve(expression) == date(2026, 2, 13)

    def test_week_number_without_weekday_is_ambiguous(self, resolver):
        # "Week 5" could be any of five class days — refuse rather than
        # defaulting to Monday or Friday.
        with pytest.raises(AmbiguousDateError) as exc:
            resolver.resolve("Week 5")
        assert "weekday" in exc.value.reason

    def test_implausible_week_number_is_unresolvable(self, resolver):
        with pytest.raises(UnresolvableDateError):
            resolver.resolve("Week 87 Friday")

    def test_week_anchoring_when_semester_starts_midweek(self):
        """Week 1 is the block *containing* the start, anchored to Monday.

        Semester starting Wednesday 2026-01-14: Week 1 Friday is Jan 16
        (same week), and Week 2 Monday is Jan 19.
        """
        midweek = DateResolver(date(2026, 1, 14))
        assert midweek.resolve("Week 1 Friday") == date(2026, 1, 16)
        assert midweek.resolve("Week 2 Monday") == date(2026, 1, 19)

    def test_sunday_first_week_convention(self):
        """Institutions that count weeks Sunday-first get a different answer,
        and that difference is configuration rather than a code change."""
        sunday_first = DateResolver(SEMESTER_START, week_start_weekday=6)
        # Week 1 now begins Sunday 2026-01-11, so Week 5 begins Feb 8 and its
        # Friday is Feb 13 — same here, but Week 1 Sunday differs:
        assert sunday_first.resolve("Week 1 Sunday") == date(2026, 1, 11)
        assert sunday_first.resolve("Week 5 Friday") == date(2026, 2, 13)


# ---------------------------------------------------------------------------
# "Nth [Weekday] of [Month]"
# ---------------------------------------------------------------------------


class TestOrdinalWeekdayPatterns:
    def test_second_tuesday_of_october(self, resolver):
        # Oct 1 2026 is a Thursday -> first Tuesday is Oct 6 -> second is Oct 13.
        assert resolver.resolve("second Tuesday of October") == date(2026, 10, 13)

    @pytest.mark.parametrize(
        "expression",
        [
            "second Tuesday of October",
            "2nd Tuesday of October",
            "the second Tuesday of October",
            "second Tuesday in October",
            "second tuesday of oct",
            "2nd Tues of Oct 2026",
        ],
    )
    def test_ordinal_spelling_variants(self, resolver, expression):
        assert resolver.resolve(expression) == date(2026, 10, 13)

    def test_first_and_last(self, resolver):
        assert resolver.resolve("first Tuesday of October") == date(2026, 10, 6)
        # November 2026 Fridays: 6, 13, 20, 27 -> last is the 27th.
        assert resolver.resolve("last Friday of November") == date(2026, 11, 27)

    def test_nonexistent_occurrence_is_unresolvable(self, resolver):
        """October 2026 has only four Mondays (5, 12, 19, 26).

        The dangerous failure would be rolling into November. We refuse.
        """
        with pytest.raises(UnresolvableDateError) as exc:
            resolver.resolve("fifth Monday of October")
        assert "no 5th occurrence" in exc.value.reason

    def test_impossible_explicit_year_is_flagged_not_crashed(self, resolver):
        """OCR turns years into rubbish, and "November 0000" is not a date.

        The month/day family already converts calendar errors into our own
        error type; the ordinal family must too. If a bare ValueError escapes
        stage 3, the orchestrator's ``except UnresolvableDateError`` misses it
        and one damaged line aborts the entire syllabus instead of being
        flagged for review.
        """
        for expression in ("last Friday of November 0000", "second Tuesday of October 0000"):
            with pytest.raises(UnresolvableDateError) as exc:
                resolver.resolve(expression)
            assert "not a valid calendar date" in exc.value.reason

    def test_explicit_year_is_respected(self):
        # Semester starting Sept 2026; "first Friday of February 2027".
        fall = DateResolver(date(2026, 9, 1))
        # Feb 1 2027 is a Monday -> first Friday is Feb 5.
        assert date(2027, 2, 1).weekday() == 0
        assert fall.resolve("first Friday of February 2027") == date(2027, 2, 5)


# ---------------------------------------------------------------------------
# Direct calendar dates
# ---------------------------------------------------------------------------


class TestDirectCalendarDates:
    @pytest.mark.parametrize(
        "expression,expected",
        [
            ("Oct 10", date(2026, 10, 10)),
            ("October 10", date(2026, 10, 10)),
            ("Oct. 10", date(2026, 10, 10)),
            ("October 10th", date(2026, 10, 10)),
            ("October 10, 2026", date(2026, 10, 10)),
            ("Oct 10 2026", date(2026, 10, 10)),
            ("10 October", date(2026, 10, 10)),
            ("10th of October", date(2026, 10, 10)),
            ("2026-10-10", date(2026, 10, 10)),
            ("10/10", date(2026, 10, 10)),
            ("10/10/2026", date(2026, 10, 10)),
            ("10/10/26", date(2026, 10, 10)),
            ("due Oct 10", date(2026, 10, 10)),
            ("due by 11:59 PM on October 10", date(2026, 10, 10)),
            ("Deadline: 10/10/2026 at 5pm", date(2026, 10, 10)),
        ],
    )
    def test_calendar_date_formats(self, resolver, expression, expected):
        assert resolver.resolve(expression) == expected

    def test_numeric_is_month_first_by_default(self, resolver):
        # 3/4 -> March 4 under the default US reading.
        assert resolver.resolve("3/4/2026") == date(2026, 3, 4)

    def test_day_first_mode(self):
        day_first = DateResolver(SEMESTER_START, day_first=True)
        assert day_first.resolve("3/4/2026") == date(2026, 4, 3)

    def test_year_rolls_forward_for_a_fall_semester(self):
        """A syllabus for a semester starting Sept 2026 that says "Feb 10"
        means February *2027* — the next occurrence, not the past one."""
        fall = DateResolver(date(2026, 9, 1))
        assert fall.resolve("Feb 10") == date(2027, 2, 10)
        assert fall.resolve("Oct 10") == date(2026, 10, 10)

    def test_invalid_calendar_date_is_unresolvable(self, resolver):
        with pytest.raises(UnresolvableDateError) as exc:
            resolver.resolve("February 30")
        assert "not a valid calendar date" in exc.value.reason

    def test_impossible_month_number_is_unresolvable(self, resolver):
        # 13/05 is not a US-format date; day-first users must opt in explicitly
        # rather than have the resolver quietly switch conventions.
        with pytest.raises(UnresolvableDateError):
            resolver.resolve("13/05/2026")


# ---------------------------------------------------------------------------
# The UnresolvableDateError path — the whole point of the module
# ---------------------------------------------------------------------------


class TestUnresolvablePath:
    @pytest.mark.parametrize(
        "expression",
        ["TBD", "tba", "T.B.D.", "To be announced", "N/A", "see Canvas", "varies", "rolling"],
    )
    def test_placeholders(self, resolver, expression):
        with pytest.raises(UnresolvableDateError) as exc:
            resolver.resolve(expression)
        assert "placeholder" in exc.value.reason

    @pytest.mark.parametrize(
        "expression",
        [
            "one week after the midterm",
            "the class before the final",
            "same week as Project 1",
            "two days prior to the exam",
        ],
    )
    def test_relative_to_another_event(self, resolver, expression):
        """Cross-task resolution is out of scope, and a plausible guess here
        would be indistinguishable from a correct answer."""
        with pytest.raises(UnresolvableDateError) as exc:
            resolver.resolve(expression)
        assert "relative to another event" in exc.value.reason

    @pytest.mark.parametrize("expression", ["", "   ", None])
    def test_empty(self, resolver, expression):
        with pytest.raises(UnresolvableDateError):
            resolver.resolve(expression)

    @pytest.mark.parametrize(
        "expression",
        [
            "Friday",  # weekday alone: which Friday?
            "sometime in the spring",
            "the last class",
            "banana",
            "assignment 3",
        ],
    )
    def test_unmatched_expressions_raise_rather_than_guess(self, resolver, expression):
        with pytest.raises(UnresolvableDateError):
            resolver.resolve(expression)

    def test_error_carries_the_original_phrase(self, resolver):
        """needs_review.json quotes this, so it must be the untouched original
        — not the lower-cased, filler-stripped internal form."""
        original = "Due by 11:59 PM, TBD"
        with pytest.raises(UnresolvableDateError) as exc:
            resolver.resolve(original)
        assert exc.value.raw_expression == original

    def test_original_phrase_survives_handler_level_failures(self, resolver):
        """Errors raised deep inside a pattern handler must still report the
        caller's phrase, not the fragment the handler saw."""
        original = "Exam: fifth Monday of October"
        with pytest.raises(UnresolvableDateError) as exc:
            resolver.resolve(original)
        assert exc.value.raw_expression == original


# ---------------------------------------------------------------------------
# Ambiguity and internal contradiction
# ---------------------------------------------------------------------------


class TestAmbiguity:
    def test_two_different_dates_in_one_expression(self, resolver):
        with pytest.raises(AmbiguousDateError) as exc:
            resolver.resolve("Oct 10 or Oct 17")
        assert "conflicting dates" in exc.value.reason

    def test_date_range_is_not_a_due_date(self, resolver):
        with pytest.raises(AmbiguousDateError) as exc:
            resolver.resolve("Oct 10-12")
        assert "range" in exc.value.reason

    def test_date_range_with_word_separator(self, resolver):
        with pytest.raises(AmbiguousDateError):
            resolver.resolve("Oct 10 through 12")

    def test_weekday_disagreeing_with_the_date(self, resolver):
        """The classic syllabus typo. Oct 10 2026 is a Saturday, so
        "Friday, Oct 10" is internally inconsistent — a human should decide
        whether the weekday or the date was the mistake."""
        with pytest.raises(AmbiguousDateError) as exc:
            resolver.resolve("Friday, Oct 10")
        assert "Friday" in exc.value.reason and "Saturday" in exc.value.reason

    def test_weekday_agreeing_with_the_date_is_fine(self, resolver):
        # Oct 9 2026 is a Friday.
        assert date(2026, 10, 9).weekday() == 4
        assert resolver.resolve("Friday, Oct 9") == date(2026, 10, 9)

    def test_redundant_expression_that_agrees_resolves(self, resolver):
        """"Week 5 Friday (Feb 13)" states the same date twice; the resolver
        cross-checks the two rather than trusting either alone."""
        assert resolver.resolve("Week 5 Friday (Feb 13)") == date(2026, 2, 13)

    def test_redundant_expression_that_disagrees_is_ambiguous(self, resolver):
        with pytest.raises(AmbiguousDateError):
            resolver.resolve("Week 5 Friday (Feb 20)")


# ---------------------------------------------------------------------------
# Plausibility window
# ---------------------------------------------------------------------------


class TestPlausibilityWindow:
    def test_ocr_damaged_year_is_rejected(self, resolver):
        """A stray "2006" from a bad scan parses perfectly and is completely
        wrong. Reject rather than create an event 20 years in the past."""
        with pytest.raises(DateOutOfRangeError) as exc:
            resolver.resolve("Oct 10, 2006")
        assert "outside the plausible window" in exc.value.reason

    def test_far_future_year_is_rejected(self, resolver):
        with pytest.raises(DateOutOfRangeError):
            resolver.resolve("Oct 10, 2099")

    def test_shortly_before_the_semester_start_is_allowed(self, resolver):
        """Orientation tasks dated just before day one are legitimate; the
        default 14-day grace window covers them."""
        assert resolver.resolve("2026-01-05") == date(2026, 1, 5)

    def test_window_is_configurable(self):
        tight = DateResolver(SEMESTER_START, max_horizon_days=30)
        with pytest.raises(DateOutOfRangeError):
            tight.resolve("2026-06-01")


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


class TestPublicAPI:
    def test_resolve_iso_returns_iso_8601(self, resolver):
        assert resolver.resolve_iso("Week 5 Friday") == "2026-02-13"

    def test_module_level_convenience_function(self):
        assert resolve_date("Week 5 Friday", SEMESTER_START) == date(2026, 2, 13)

    def test_error_hierarchy_lets_callers_catch_one_type(self, resolver):
        """The orchestrator catches only UnresolvableDateError; the subclasses
        must therefore remain subclasses."""
        assert issubclass(AmbiguousDateError, UnresolvableDateError)
        assert issubclass(DateOutOfRangeError, UnresolvableDateError)
        with pytest.raises(UnresolvableDateError):
            resolver.resolve("Oct 10 or Oct 17")  # actually AmbiguousDateError

    def test_invalid_week_start_weekday_rejected_at_construction(self):
        with pytest.raises(ValueError):
            DateResolver(SEMESTER_START, week_start_weekday=9)

    def test_resolver_is_reusable_across_many_expressions(self, resolver):
        """No hidden per-call state: resolving in any order gives the same
        answers."""
        first = [resolver.resolve("Week 5 Friday"), resolver.resolve("Oct 10")]
        second = [resolver.resolve("Oct 10"), resolver.resolve("Week 5 Friday")]
        assert first == list(reversed(second))


# ---------------------------------------------------------------------------
# Multi-day spans
#
# Added after running a real B.Tech semester calendar through the pipeline:
# every exam period ("Sept 24 - Sept 30, 2026") was refused as ambiguous, but
# an exam *window* is a genuine multi-day event, not an unclear deadline.
# ---------------------------------------------------------------------------


class TestDateSpans:
    @pytest.mark.parametrize(
        "expression,start,end",
        [
            # The four periods from the real syllabus that motivated this.
            ("Sept 24 - Sept 30, 2026", date(2026, 9, 24), date(2026, 9, 30)),
            ("Nov 23 - Nov 28, 2026", date(2026, 11, 23), date(2026, 11, 28)),
            ("Dec 9 - Dec 23, 2026", date(2026, 12, 9), date(2026, 12, 23)),
            # Crosses a month boundary.
            ("Nov 30 - Dec 7, 2026", date(2026, 11, 30), date(2026, 12, 7)),
            # Shorthand: the closing day inherits the opening month.
            ("Oct 10-12", date(2026, 10, 10), date(2026, 10, 12)),
            ("Oct 10 to 12", date(2026, 10, 10), date(2026, 10, 12)),
            ("Oct 10 through 12", date(2026, 10, 10), date(2026, 10, 12)),
            # Both sides fully specified.
            ("Oct 10 - Oct 17", date(2026, 10, 10), date(2026, 10, 17)),
        ],
    )
    def test_ranges_resolve_to_spans(self, resolver, expression, start, end):
        span = resolver.resolve_span(expression)
        assert span.is_range is True
        assert (span.start, span.end) == (start, end)

    def test_span_reports_inclusive_length(self, resolver):
        # Dec 9 through Dec 23 inclusive is 15 days, not 14.
        assert resolver.resolve_span("Dec 9 - Dec 23, 2026").days == 15

    def test_single_date_is_a_span_of_one(self, resolver):
        span = resolver.resolve_span("Oct 10")
        assert span.is_range is False
        assert span.end is None
        assert span.days == 1

    def test_a_real_contradiction_is_still_refused(self, resolver):
        """The separator is what distinguishes a span from a contradiction:
        "Oct 10 - Oct 17" is a period, "Oct 10 or Oct 17" is the syllabus
        disagreeing with itself and must still reach a human."""
        for expression in ("Oct 10 or Oct 17", "Oct 10 and Oct 17"):
            with pytest.raises(AmbiguousDateError) as exc:
                resolver.resolve_span(expression)
            assert "conflicting dates" in exc.value.reason

    def test_resolve_stays_strict_about_ranges(self, resolver):
        """``resolve`` promises a single day, so a span is still an error
        there — the range-aware callers use ``resolve_span``."""
        with pytest.raises(AmbiguousDateError) as exc:
            resolver.resolve("Dec 9 - Dec 23, 2026")
        assert "range" in exc.value.reason

    def test_backwards_range_is_refused(self, resolver):
        with pytest.raises(AmbiguousDateError) as exc:
            resolver.resolve_span("Oct 17 - Oct 10")
        assert "ends before it starts" in exc.value.reason

    def test_impossible_closing_day_does_not_invent_one(self, resolver):
        """"Feb 27-30" has no 30th. Silently rolling into March would be
        exactly the guess this module refuses."""
        with pytest.raises(UnresolvableDateError):
            resolver.resolve_span("Feb 27-30")

    def test_both_ends_are_range_checked(self, resolver):
        """A span whose far end lands outside the plausibility window is OCR
        damage, the same as a single date would be."""
        with pytest.raises(DateOutOfRangeError):
            resolver.resolve_span("Oct 10, 2026 - Oct 10, 2099")
