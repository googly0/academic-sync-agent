"""Pydantic models: the contract between pipeline stages.

Two models matter here, and the distinction between them is deliberate:

``RawExtractedTask``
    What the *LLM* produces (stage 2). Everything is optional and nothing is
    resolved — this is a faithful, unopinionated transcription of what the
    syllabus literally says. It is also used as the structured-output schema
    handed to the model, so it must stay JSON-Schema-simple (no numeric
    constraints, no recursion).

``AcademicTask``
    The *validated* model that gates stages 2/3 → stage 5 (calendar sync).
    It is intentionally permissive about missing data: a task with no due date
    must still survive validation so it can be **flagged**, not dropped. The
    ``requires_manual_review`` / ``review_reason`` fields are computed by a
    model validator so the flagging rules live in exactly one place.

Design rule for the whole file: never let a bad record vanish. A task that
cannot be understood is flagged and written to ``needs_review.json``; it is
never silently guessed at and never silently discarded.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

# --------------------------------------------------------------------------
# Review-reason codes.
#
# The spec calls for exactly three cases that force manual review. Keeping
# them as named constants means the orchestrator, the tests, and any future
# dashboard can branch on a stable string instead of matching prose.
# --------------------------------------------------------------------------
REVIEW_MISSING_FIELDS = "missing_required_fields"
REVIEW_CONTRADICTION = "contradiction_detected"
REVIEW_UNRESOLVED_DATE = "unresolvable_date"

#: Fields that must be present for a task to be safe to put on someone's
#: calendar. ``exact_due_date`` is handled separately (it has its own, more
#: specific review code) so it is not listed here.
#:
#: ``task_description`` is deliberately absent. Many real syllabi state a
#: deadline in one terse line with no separate description sentence — e.g.
#: "Problem Set 1 (10%) due Week 3 Friday" — and blocking sync on that would
#: flag most ordinary tasks for a reason that isn't actually a data-quality
#: problem. A missing description is a worse calendar entry, not an unsafe
#: one; a wrong course, task name, weight, or date is what actually
#: justifies routing to a human.
REQUIRED_FOR_SYNC = ("course_name", "task_name", "grading_weight")


def _normalise_for_key(value: str) -> str:
    """Lower-case and collapse whitespace so trivial formatting differences
    between two runs of the LLM don't produce two different dedupe keys.

    Deliberately conservative: we do *not* strip punctuation, because
    "Problem Set 3" and "Problem Set #3" may genuinely be different tasks in
    a badly written syllabus and we would rather create a duplicate event
    (visible, fixable) than silently merge two deadlines (invisible, harmful).
    """
    return re.sub(r"\s+", " ", value.strip().lower())


class RawExtractedTask(BaseModel):
    """Stage-2 output: what the syllabus *says*, before any interpretation.

    Every field is optional because a syllabus may simply not state it, and
    "the syllabus didn't say" is information we want to preserve and flag —
    not an error that aborts extraction of the other twelve tasks on the page.
    """

    # ``extra="forbid"`` maps to JSON Schema ``additionalProperties: false``,
    # which the structured-outputs API requires.
    model_config = ConfigDict(extra="forbid")

    course_name: Optional[str] = Field(
        default=None,
        description="Course name or code exactly as written in the syllabus, e.g. 'CS 4820' or 'Organic Chemistry II'.",
    )
    task_name: Optional[str] = Field(
        default=None,
        description="Short name of the assignment, exam, quiz, or project, e.g. 'Problem Set 3' or 'Midterm Exam'.",
    )
    raw_date_expression: Optional[str] = Field(
        default=None,
        description=(
            "The due-date phrase copied VERBATIM from the source text and left "
            "completely unparsed, e.g. 'Week 5 Friday', 'second Tuesday of "
            "October', 'Oct 10', 'TBD'. Never compute or normalise a date."
        ),
    )
    grading_weight: Optional[str] = Field(
        default=None,
        description="Weight toward the final grade exactly as stated, e.g. '20%', '150 points', 'pass/fail'.",
    )
    task_description: Optional[str] = Field(
        default=None,
        description="One or two sentences describing the deliverable, drawn from the syllabus text.",
    )
    contradiction_detected: bool = Field(
        default=False,
        description=(
            "True ONLY when the source text states two genuinely different due "
            "dates for this same task (e.g. prose says Oct 10, the table says "
            "Oct 17). Do not attempt to decide which one is correct."
        ),
    )
    contradiction_quotes: List[str] = Field(
        default_factory=list,
        description=(
            "When contradiction_detected is true, the two (or more) conflicting "
            "quotes copied verbatim from the source text. Empty otherwise."
        ),
    )
    source_page: Optional[int] = Field(
        default=None,
        description="1-indexed PDF page the task was found on, when identifiable.",
    )


class SyllabusExtraction(BaseModel):
    """Top-level structured-output envelope handed to the LLM.

    Structured outputs need an object at the root, so the list of tasks is
    wrapped rather than returned bare.
    """

    model_config = ConfigDict(extra="forbid")

    tasks: List[RawExtractedTask] = Field(
        default_factory=list,
        description="Every graded task, exam, and deadline found in this chunk of the syllabus.",
    )


class AcademicTask(BaseModel):
    """The validated boundary model. Only instances of this reach stage 5.

    Validation here is about *routing*, not rejection: the model validator
    decides whether a task is safe to sync or must be flagged for a human.
    """

    model_config = ConfigDict(extra="forbid")

    # --- Core payload -----------------------------------------------------
    course_name: Optional[str] = None
    task_name: Optional[str] = None
    #: Absolute due date, ISO 8601 on serialisation. ``None`` means stage 3
    #: refused to guess — the task is flagged, never given a fabricated date.
    exact_due_date: Optional[date] = None
    grading_weight: Optional[str] = None
    task_description: Optional[str] = None
    #: Kept verbatim so a human reviewing needs_review.json can see exactly
    #: what the syllabus said without reopening the PDF.
    raw_date_expression: Optional[str] = None

    # --- Review flags -----------------------------------------------------
    requires_manual_review: bool = False
    review_reason: Optional[str] = None
    contradiction_detected: bool = False
    contradiction_quotes: List[str] = Field(default_factory=list)

    # --- Provenance (useful in review, ignored by sync) -------------------
    source_page: Optional[int] = None
    #: Set by the orchestrator when stage 3 raised; carried through so the
    #: review file explains *why* the phrase could not be resolved.
    date_resolution_error: Optional[str] = None

    @model_validator(mode="after")
    def _apply_review_rules(self) -> "AcademicTask":
        """Compute ``requires_manual_review`` / ``review_reason``.

        Exactly the three cases from the spec, in a fixed order so the reason
        string is deterministic and testable:

        1. missing required fields
        2. contradictory date statements in the source
        3. an unresolvable ``raw_date_expression``

        Runs on *every* construction (including ``model_copy(update=...)``),
        so a caller cannot forge an unflagged task by setting the booleans by
        hand — the validator recomputes them from the underlying data.
        """
        reasons: List[str] = []

        missing = [
            name
            for name in REQUIRED_FOR_SYNC
            if not (getattr(self, name) or "").strip()
        ]
        if missing:
            reasons.append(f"{REVIEW_MISSING_FIELDS}: {', '.join(missing)}")

        if self.contradiction_detected:
            quotes = " | ".join(q.strip() for q in self.contradiction_quotes if q.strip())
            detail = f" ({quotes})" if quotes else ""
            reasons.append(f"{REVIEW_CONTRADICTION}: source states conflicting dates{detail}")

        if self.exact_due_date is None:
            detail = self.date_resolution_error or "no absolute date could be derived"
            phrase = self.raw_date_expression or "<no date expression found>"
            reasons.append(f"{REVIEW_UNRESOLVED_DATE}: {phrase!r} — {detail}")

        # Assign through ``object.__setattr__``-free normal assignment: we are
        # in mode="after", so mutating self is supported and does not re-trigger
        # validation (Pydantic v2 only re-runs validators on assignment when
        # ``validate_assignment`` is enabled, which it is not here).
        self.requires_manual_review = bool(reasons)
        self.review_reason = "; ".join(reasons) if reasons else None
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_dedupe_key(self) -> Optional[str]:
        """Stable identity for (course, task, exact date).

        Used two ways in stage 5: as the local checkpoint key in
        ``sync_state.json``, and as a private extended property on the Google
        Calendar event so idempotency survives a lost/deleted state file.

        Returns ``None`` when there is no resolved date — such a task is never
        synced, so it has no calendar identity. Callers must treat ``None`` as
        "not syncable" rather than as a key.
        """
        if self.exact_due_date is None or not self.course_name or not self.task_name:
            return None
        payload = "|".join(
            (
                _normalise_for_key(self.course_name),
                _normalise_for_key(self.task_name),
                self.exact_due_date.isoformat(),
            )
        )
        # 32 hex chars is far more than enough for per-user syllabus volumes
        # and keeps the value inside Google's extended-property length limits.
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    @property
    def is_syncable(self) -> bool:
        """True only when the task passed every gate and has a dedupe key."""
        return not self.requires_manual_review and self.sync_dedupe_key is not None

    @classmethod
    def from_raw(
        cls,
        raw: RawExtractedTask,
        *,
        exact_due_date: Optional[date] = None,
        date_resolution_error: Optional[str] = None,
    ) -> "AcademicTask":
        """Build a validated task from stage-2 output plus stage-3's verdict.

        The date and the failure reason are passed in rather than computed
        here, because date resolution is a separate, deterministic, separately
        testable module — this model must not know how dates are parsed.
        """
        return cls(
            course_name=raw.course_name,
            task_name=raw.task_name,
            exact_due_date=exact_due_date,
            grading_weight=raw.grading_weight,
            task_description=raw.task_description,
            raw_date_expression=raw.raw_date_expression,
            contradiction_detected=raw.contradiction_detected,
            contradiction_quotes=list(raw.contradiction_quotes),
            source_page=raw.source_page,
            date_resolution_error=date_resolution_error,
        )
