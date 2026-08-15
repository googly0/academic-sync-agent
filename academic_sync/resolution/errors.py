"""Failure types for stage 3.

There is one base error and two subclasses. Callers that only care about
"could this be resolved?" catch ``UnresolvableDateError``; callers that want
to explain *why* to a human can branch on the subclass or read ``.reason``.

Every error carries the **original** phrase, unmodified, so the review file
can quote what the syllabus actually said.
"""

from __future__ import annotations

from typing import Optional


class UnresolvableDateError(ValueError):
    """A date expression did not match any known, safe-to-resolve pattern.

    Raised instead of returning a best guess. The pipeline turns this into
    ``requires_manual_review=True``; it never becomes a calendar event.
    """

    def __init__(self, raw_expression: Optional[str], reason: str) -> None:
        self.raw_expression = raw_expression or ""
        self.reason = reason
        super().__init__(f"cannot resolve {self.raw_expression!r}: {reason}")


class AmbiguousDateError(UnresolvableDateError):
    """The expression matched more than one plausible date, or matched a
    pattern that is under-specified (e.g. "Week 5" with no weekday), or
    contains internally inconsistent components (e.g. "Friday, Oct 10" where
    Oct 10 is a Saturday).

    Ambiguity is a *stronger* signal than "unparseable" — it usually means the
    syllabus itself is unclear and a human genuinely needs to look.
    """


class DateOutOfRangeError(UnresolvableDateError):
    """The expression parsed cleanly but landed implausibly far from the
    semester — almost always OCR damage ("Oct 10, 2006") or a stray number
    that happened to look like a date. Treated as unresolvable rather than
    trusted.
    """
