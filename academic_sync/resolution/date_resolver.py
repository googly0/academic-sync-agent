"""Stage 3 — deterministic date resolution.

Takes a ``raw_date_expression`` (whatever the syllabus literally said) plus a
``semester_start_date`` and returns an absolute ``datetime.date``. No LLM, no
network, no heuristic "closest match" fallback.

Design principles, in priority order
------------------------------------
1. **Never guess.** An expression that does not match a whitelisted pattern
   raises :class:`UnresolvableDateError`. There is deliberately no call to
   ``dateutil.parser.parse(fuzzy=True)`` anywhere in this file — fuzzy parsing
   is exactly the silent-wrong-answer machine this module exists to avoid.
2. **Contradictions are errors, not tie-breaks.** If an expression yields two
   different dates ("Oct 10 or Oct 17"), or names a weekday that disagrees
   with the calendar date it also names ("Friday, Oct 10" in a year where
   Oct 10 is a Saturday), we raise rather than pick.
3. **Under-specification is an error.** "Week 5" without a weekday could mean
   any of five days; it raises rather than defaulting to Monday or Friday.
4. **Implausible results are errors.** A parsed date outside a window around
   the semester is treated as OCR damage.

How pattern matching works
--------------------------
Patterns are tried in a fixed *ladder*, most specific first. Each family scans
the working text, and the spans it consumed are then blanked out (replaced
with spaces, preserving indices) before the next family runs. That masking is
what stops "second Tuesday of October" from also matching a month/day pattern
on the trailing "October".

After the ladder, all surviving candidates must agree. If they do, that single
date is the answer — which means a redundant expression like
"Week 5 Friday (Feb 13)" is *cross-checked* by this module rather than merely
parsed by it.

Week numbering convention (documented, not inferred)
----------------------------------------------------
Week 1 is the 7-day block containing ``semester_start_date``, anchored to the
configured ``week_start_weekday`` (Monday by default). So if the semester
starts Wednesday 14 Jan, "Week 1 Friday" is Friday 16 Jan, and "Week 2 Monday"
is Monday 19 Jan. If your institution counts weeks differently, change
``week_start_weekday`` — do not change the caller's semester start.
"""

from __future__ import annotations

import calendar as _calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Dict, List, Optional, Pattern, Sequence, Tuple

from .errors import AmbiguousDateError, DateOutOfRangeError, UnresolvableDateError

# ---------------------------------------------------------------------------
# Vocabulary tables
# ---------------------------------------------------------------------------

#: Weekday spellings/abbreviations → ``date.weekday()`` index (Monday == 0).
WEEKDAY_NAMES: Dict[str, int] = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tues": 1, "tue": 1,
    "wednesday": 2, "weds": 2, "wed": 2,
    "thursday": 3, "thurs": 3, "thur": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

#: Month spellings/abbreviations → month number.
MONTH_NAMES: Dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

#: Ordinal words/numerals → occurrence index. ``-1`` means "last".
ORDINAL_WORDS: Dict[str, int] = {
    "first": 1, "1st": 1,
    "second": 2, "2nd": 2,
    "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5,
    "last": -1, "final": -1,
}


def _alternation(keys: Sequence[str]) -> str:
    """Build a regex alternation with longest-first ordering.

    Order matters: without it, ``mon|monday`` would match only "mon" inside
    "monday" and leave a stray "day" behind.
    """
    return "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))


_WD = _alternation(list(WEEKDAY_NAMES))
_MO = _alternation(list(MONTH_NAMES))
_ORD = _alternation(list(ORDINAL_WORDS))

# ---------------------------------------------------------------------------
# Pre-flight regexes (checked before the pattern ladder runs)
# ---------------------------------------------------------------------------

#: Explicit "we don't know yet" markers. These are common and unambiguous, so
#: we produce a *specific* reason for them rather than a generic parse failure.
PLACEHOLDER_RE = re.compile(
    r"\b(?:tbd|tba|t\.?b\.?[ad]\.?|n/?a|to be (?:announced|determined|scheduled)|"
    r"see (?:canvas|blackboard|moodle|website|syllabus)|varies|staggered|rolling|"
    r"ongoing|as announced|weekly|biweekly)\b",
    re.IGNORECASE,
)

#: Expressions anchored to *another* event ("one week after the midterm").
#: Resolving these would require cross-task reasoning, which this module
#: deliberately does not do — so they are flagged, not approximated.
RELATIVE_RE = re.compile(
    r"\b(?:after|before|prior to|following|preceding|later|earlier|"
    r"same (?:day|week) as|day of)\b",
    re.IGNORECASE,
)

#: Time-of-day noise. Stripped early so "11:59" is never mistaken for a date.
TIME_RE = re.compile(
    r"(?:\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\b\d{1,2}:\d{2}\b|"
    r"\b(?:noon|midnight|eod|end of day)\b)",
    re.IGNORECASE,
)

#: Filler that carries no date information. Only words that cannot change the
#: meaning of a date are listed — notably NOT "before"/"after", which are
#: handled by ``RELATIVE_RE`` above because removing them would silently turn
#: "the day before Oct 10" into "Oct 10".
FILLER_RE = re.compile(
    r"\b(?:due date|due|deadline|submitted|submission|submit|hand in|posted|"
    r"no later than|at|on|by|the|of the)\b",
    re.IGNORECASE,
)

#: A range tail immediately following a matched date: "Oct 10-12",
#: "Oct 10 to 12". A range is not a due date, so it is ambiguous.
RANGE_SUFFIX_RE = re.compile(
    r"^\s*(?:[-–—]\s*|(?:to|through|thru|until|til|till)\s+)\d{1,2}\b",
    re.IGNORECASE,
)

#: Any leftover standalone weekday, used for the consistency cross-check.
LEFTOVER_WEEKDAY_RE = re.compile(rf"\b({_WD})s?\b", re.IGNORECASE)


@dataclass(frozen=True)
class _Candidate:
    """One date produced by one pattern match, with its source span."""

    value: date
    span: Tuple[int, int]
    family: str


class DateResolver:
    """Resolves relative and absolute date expressions against a semester.

    Instances are cheap and stateless-per-call; construct one per pipeline run
    and reuse it. All configuration is explicit — there are no module-level
    globals that could make two callers behave differently.

    Args:
        semester_start_date: Anchor for week numbering and year inference.
        week_start_weekday: Which weekday begins an academic week
            (0 = Monday, the default; 6 = Sunday for institutions that count
            weeks Sunday-first).
        grace_days_before: How far *before* the semester start a resolved date
            may fall before it is considered implausible. A small window is
            allowed because syllabi sometimes list an orientation task dated
            just before the official start.
        max_horizon_days: How far *after* the semester start a resolved date
            may fall. 400 days comfortably covers a full-year course while
            still catching OCR damage like a 20-year-old year number.
        day_first: Interpretation of purely numeric dates. ``False`` (default)
            reads ``10/17`` as **October 17** (US convention). Set ``True`` for
            ``17/10`` = 17 October. Note this only affects the numeric family;
            named-month expressions are unaffected.
    """

    def __init__(
        self,
        semester_start_date: date,
        *,
        week_start_weekday: int = 0,
        grace_days_before: int = 14,
        max_horizon_days: int = 400,
        day_first: bool = False,
    ) -> None:
        if not 0 <= week_start_weekday <= 6:
            raise ValueError("week_start_weekday must be 0 (Mon) through 6 (Sun)")
        self.semester_start_date = semester_start_date
        self.week_start_weekday = week_start_weekday
        self.grace_days_before = grace_days_before
        self.max_horizon_days = max_horizon_days
        self.day_first = day_first

        # Monday-of-week-1 (or Sunday-of, per config): the fixed point that all
        # "Week N" arithmetic hangs off.
        offset = (semester_start_date.weekday() - week_start_weekday) % 7
        self._week_one_start = semester_start_date - timedelta(days=offset)

        self._ladder = self._build_ladder()

    # -- public API --------------------------------------------------------

    def resolve(self, raw_expression: Optional[str]) -> date:
        """Resolve ``raw_expression`` to an absolute date.

        Raises:
            UnresolvableDateError: no whitelisted pattern matched, or the
                expression is a placeholder ("TBD") or relative to another
                event.
            AmbiguousDateError: the expression yields conflicting dates, is
                under-specified, or is a date range.
            DateOutOfRangeError: the expression parsed but landed outside the
                plausibility window around the semester.
        """
        original = raw_expression or ""
        if not original.strip():
            raise UnresolvableDateError(original, "expression is empty")

        text = self._normalise(original)
        if not text.strip():
            raise UnresolvableDateError(
                original, "nothing left after removing times and filler words"
            )

        # Pre-flight checks: catch the two big classes of "legitimately
        # unresolvable" before spending effort on pattern matching, so the
        # reason we report is specific and actionable.
        if PLACEHOLDER_RE.search(text):
            raise UnresolvableDateError(
                original, "expression is a placeholder (TBD/TBA/varies) — no date stated"
            )
        if RELATIVE_RE.search(text):
            raise UnresolvableDateError(
                original,
                "expression is relative to another event; cross-task resolution is not performed",
            )

        candidates, residue = self._scan(original, text)

        if not candidates:
            raise UnresolvableDateError(
                original, "no known date pattern matched (not a week, ordinal, or calendar date)"
            )

        distinct = sorted({c.value for c in candidates})
        if len(distinct) > 1:
            rendered = ", ".join(d.isoformat() for d in distinct)
            raise AmbiguousDateError(
                original, f"expression yields multiple conflicting dates: {rendered}"
            )

        resolved = distinct[0]

        # A range ("Oct 10-12") parses to a single leading date, which would
        # otherwise sail through. Detect the trailing range marker explicitly.
        self._reject_ranges(original, text, candidates)

        # Cross-check any weekday word the patterns did not consume. This is
        # what catches the very common syllabus typo "Friday, Oct 10" where
        # Oct 10 is actually a Saturday.
        self._check_weekday_agreement(original, residue, resolved)

        self._check_window(original, resolved)
        return resolved

    def resolve_iso(self, raw_expression: Optional[str]) -> str:
        """Convenience wrapper returning an ISO 8601 (``YYYY-MM-DD``) string."""
        return self.resolve(raw_expression).isoformat()

    # -- normalisation -----------------------------------------------------

    def _normalise(self, expression: str) -> str:
        """Lower-case, strip times and filler, collapse whitespace.

        Index-preservation is *not* required here (this runs before any spans
        are recorded), so it is safe to change the string's length.
        """
        text = expression.lower()
        text = text.replace("–", "-").replace("—", "-")  # en/em dash
        text = TIME_RE.sub(" ", text)
        text = FILLER_RE.sub(" ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    # -- the pattern ladder ------------------------------------------------

    def _build_ladder(self) -> List[Tuple[str, Pattern[str], Callable[[re.Match], date]]]:
        """Ordered (family, pattern, handler) triples.

        Ordering is load-bearing. Each entry consumes its spans before the next
        runs, so more specific families must come first — otherwise
        "second Tuesday of October" would be partly eaten by the month/day
        family and resolve to nonsense.
        """
        return [
            # "Friday of Week 5" — weekday first.
            (
                "week",
                re.compile(rf"\b({_WD})s?\s+of\s+(?:week|wk)\s*#?\s*(\d{{1,2}})\b", re.IGNORECASE),
                lambda m: self._from_week(m.group(2), m.group(1)),
            ),
            # "Week 5 Friday", "Wk 5", "Week 5" (weekday optional → ambiguous).
            (
                "week",
                re.compile(
                    # The optional trailing weekday tolerates "Week 5 Friday",
                    # "Week 5, Friday", "Week 5: Friday" and "Week 5 (Friday)".
                    rf"\b(?:week|wk)\s*#?\s*(\d{{1,2}})\b(?:\s*[,:\-(]?\s*({_WD})s?\b\)?)?",
                    re.IGNORECASE,
                ),
                lambda m: self._from_week(m.group(1), m.group(2)),
            ),
            # "second Tuesday of October", "last Friday in November [2026]".
            (
                "ordinal",
                re.compile(
                    rf"\b({_ORD})\s+({_WD})s?\s+(?:of|in)\s+({_MO})\.?(?:\s*,?\s*(\d{{4}}))?\b",
                    re.IGNORECASE,
                ),
                lambda m: self._from_ordinal(
                    m.group(1), m.group(2), m.group(3), m.group(4)
                ),
            ),
            # ISO 8601: "2026-10-17".
            (
                "iso",
                re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)"),
                lambda m: self._build_date(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(0)
                ),
            ),
            # Numeric: "10/17", "10/17/2026", "10/17/26". Slash only — a dash
            # separator is not accepted because "Oct 10-12" ranges make it
            # genuinely ambiguous, and guessing is not on the table.
            (
                "numeric",
                re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?!\d)"),
                lambda m: self._from_numeric(m.group(1), m.group(2), m.group(3), m.group(0)),
            ),
            # "Oct 10", "October 10th, 2026", "Oct. 10".
            (
                "month_day",
                re.compile(
                    rf"\b({_MO})\.?\s+(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)"
                    rf"(?:\s*,?\s*(\d{{4}}))?",
                    re.IGNORECASE,
                ),
                lambda m: self._from_month_day(m.group(1), m.group(2), m.group(3), m.group(0)),
            ),
            # "10 October", "10th of October 2026".
            (
                "day_month",
                re.compile(
                    rf"\b(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MO})\.?"
                    rf"(?:\s*,?\s*(\d{{4}}))?\b",
                    re.IGNORECASE,
                ),
                lambda m: self._from_month_day(m.group(2), m.group(1), m.group(3), m.group(0)),
            ),
        ]

    def _scan(self, original: str, text: str) -> Tuple[List[_Candidate], str]:
        """Run the ladder over ``text``, masking consumed spans as we go.

        Returns the candidates found and the final residue (the text with every
        matched span blanked out), which the weekday cross-check inspects.
        """
        working = text
        candidates: List[_Candidate] = []

        for family, pattern, handler in self._ladder:
            matches = list(pattern.finditer(working))
            if not matches:
                continue
            for match in matches:
                # Handlers raise on genuinely invalid input (Feb 30, "fifth
                # Monday" in a month with four). We let that propagate — a
                # malformed date is a loud failure, not a skipped candidate —
                # but re-stamp the error with the *original* phrase so the
                # review file quotes what the syllabus actually said rather
                # than the fragment the handler happened to see.
                try:
                    value = handler(match)
                except UnresolvableDateError as exc:
                    raise type(exc)(original, exc.reason) from exc
                candidates.append(_Candidate(value=value, span=match.span(), family=family))
            working = _mask(working, [m.span() for m in matches])

        return candidates, working

    # -- family handlers ---------------------------------------------------

    def _from_week(self, week_str: str, weekday_str: Optional[str]) -> date:
        """"Week N [Weekday]" → absolute date."""
        week_number = int(week_str)
        if week_number < 1 or week_number > 53:
            raise UnresolvableDateError(
                f"week {week_number}", "week number outside the plausible range 1–53"
            )
        if weekday_str is None:
            # "Week 5" alone could be any of seven days. Refuse rather than
            # inventing a convention the syllabus never stated.
            raise AmbiguousDateError(
                f"week {week_number}",
                "week number given without a weekday; the intended day is ambiguous",
            )

        weekday = WEEKDAY_NAMES[weekday_str.lower()]
        week_start = self._week_one_start + timedelta(days=(week_number - 1) * 7)
        return week_start + timedelta(days=(weekday - self.week_start_weekday) % 7)

    def _from_ordinal(
        self, ordinal_str: str, weekday_str: str, month_str: str, year_str: Optional[str]
    ) -> date:
        """"Nth [Weekday] of [Month] [Year]" → absolute date."""
        n = ORDINAL_WORDS[ordinal_str.lower()]
        weekday = WEEKDAY_NAMES[weekday_str.lower()]
        month = MONTH_NAMES[month_str.lower().rstrip(".")]
        label = f"{ordinal_str} {weekday_str} of {month_str}"

        if year_str:
            return _nth_weekday_of_month(int(year_str), month, n, weekday, label)

        # No year stated: try the semester's year, then the next one, and take
        # the first that is not already in the past relative to the semester.
        # This is inference, but it is *bounded* and checked against the
        # plausibility window afterwards.
        for year in (self.semester_start_date.year, self.semester_start_date.year + 1):
            candidate = _nth_weekday_of_month(year, month, n, weekday, label)
            if candidate >= self.semester_start_date - timedelta(days=self.grace_days_before):
                return candidate
        raise UnresolvableDateError(label, "could not place this month within the semester window")

    def _from_numeric(
        self, first: str, second: str, year_str: Optional[str], source: str
    ) -> date:
        """"10/17" or "17/10" depending on ``day_first``."""
        if self.day_first:
            day, month = int(first), int(second)
        else:
            month, day = int(first), int(second)

        if year_str is None:
            return self._infer_year_for(month, day, source)

        year = int(year_str)
        if year < 100:  # "10/17/26"
            year += 2000
        return self._build_date(year, month, day, source)

    def _from_month_day(
        self, month_str: str, day_str: str, year_str: Optional[str], source: str
    ) -> date:
        """Named-month forms, in either word order."""
        month = MONTH_NAMES[month_str.lower().rstrip(".")]
        day = int(day_str)
        if year_str is None:
            return self._infer_year_for(month, day, source)
        return self._build_date(int(year_str), month, day, source)

    # -- helpers -----------------------------------------------------------

    def _build_date(self, year: int, month: int, day: int, source: str) -> date:
        """Construct a date, converting calendar errors into our error type."""
        try:
            return date(year, month, day)
        except ValueError as exc:  # month 13, Feb 30, day 0, ...
            raise UnresolvableDateError(source, f"not a valid calendar date ({exc})") from exc

    def _infer_year_for(self, month: int, day: int, source: str) -> date:
        """Pick the year for a year-less date: semester year, else the next.

        A syllabus that says "Oct 10" for a semester starting January 2026
        means October 2026; one that says "Feb 10" for a semester starting
        September 2026 means February 2027. Anything that cannot be placed in
        that two-year span is a failure, not a third guess.
        """
        for year in (self.semester_start_date.year, self.semester_start_date.year + 1):
            candidate = self._build_date(year, month, day, source)
            if candidate >= self.semester_start_date - timedelta(days=self.grace_days_before):
                return candidate
        raise UnresolvableDateError(source, "could not place this date within the semester window")

    def _reject_ranges(
        self, original: str, text: str, candidates: Sequence[_Candidate]
    ) -> None:
        """Raise if a matched date is immediately followed by a range tail.

        ``text`` and the candidate spans share an index space because masking
        replaces characters with spaces rather than deleting them.
        """
        for candidate in candidates:
            tail = text[candidate.span[1] :]
            if RANGE_SUFFIX_RE.match(tail):
                raise AmbiguousDateError(
                    original,
                    "expression describes a date range, not a single due date",
                )

    def _check_weekday_agreement(self, original: str, residue: str, resolved: date) -> None:
        """Cross-check a leftover weekday word against the resolved date.

        "Friday, Oct 10" only consumes "Oct 10"; the dangling "Friday" is a
        free consistency check the syllabus handed us. If it disagrees, the
        source is internally inconsistent and a human should look.
        """
        match = LEFTOVER_WEEKDAY_RE.search(residue)
        if not match:
            return
        stated = WEEKDAY_NAMES[match.group(1).lower()]
        if stated != resolved.weekday():
            stated_name = _calendar.day_name[stated]
            actual_name = _calendar.day_name[resolved.weekday()]
            raise AmbiguousDateError(
                original,
                f"expression says {stated_name} but {resolved.isoformat()} is a {actual_name}",
            )

    def _check_window(self, original: str, resolved: date) -> None:
        """Reject dates implausibly far from the semester (usually OCR noise)."""
        earliest = self.semester_start_date - timedelta(days=self.grace_days_before)
        latest = self.semester_start_date + timedelta(days=self.max_horizon_days)
        if not earliest <= resolved <= latest:
            raise DateOutOfRangeError(
                original,
                f"{resolved.isoformat()} falls outside the plausible window "
                f"{earliest.isoformat()}..{latest.isoformat()}",
            )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _mask(text: str, spans: Sequence[Tuple[int, int]]) -> str:
    """Blank out ``spans`` with spaces, preserving every other index.

    Index preservation is what lets later stages (range detection) reason
    about positions recorded by earlier stages.
    """
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = " "
    return "".join(chars)


def _nth_weekday_of_month(year: int, month: int, n: int, weekday: int, label: str) -> date:
    """Return the nth (or last, when ``n == -1``) ``weekday`` of a month.

    Raises ``UnresolvableDateError`` when the requested occurrence does not
    exist — "fifth Monday of October 2026" has only four, and inventing one in
    November would be exactly the kind of silent guess this module forbids.
    """
    # Guard the calendar lookups the same way ``_build_date`` guards its own:
    # an OCR-damaged year ("last Friday of November 0000") otherwise escapes as
    # a bare ValueError and kills the whole run, instead of flagging one task.
    # Nothing inside this block raises UnresolvableDateError, which is itself a
    # ValueError and must not be caught and relabelled here.
    try:
        days_in_month = _calendar.monthrange(year, month)[1]
        first = date(year, month, 1)
    except ValueError as exc:
        raise UnresolvableDateError(label, f"not a valid calendar date ({exc})") from exc

    if n == -1:
        last = date(year, month, days_in_month)
        return last - timedelta(days=(last.weekday() - weekday) % 7)

    day_of_month = 1 + ((weekday - first.weekday()) % 7) + (n - 1) * 7
    if day_of_month > days_in_month:
        raise UnresolvableDateError(
            label,
            f"there is no {n}th occurrence of that weekday in {_calendar.month_name[month]} {year}",
        )
    return date(year, month, day_of_month)


def resolve_date(
    raw_expression: Optional[str], semester_start_date: date, **kwargs: object
) -> date:
    """One-shot convenience wrapper around :class:`DateResolver`.

    Prefer constructing a ``DateResolver`` when resolving many expressions
    against the same semester — it precomputes the week anchor and the ladder.
    """
    return DateResolver(semester_start_date, **kwargs).resolve(raw_expression)  # type: ignore[arg-type]
