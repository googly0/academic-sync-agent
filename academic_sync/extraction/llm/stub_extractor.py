"""A deterministic, offline backend for smoke-testing the plumbing.

`--llm-backend stub` exercises stages 1, 3, 4 and the dry-run path of stage 5
without an API key, a network connection, or a local model. It is a test
fixture, not an extractor: it does no NLP whatsoever, it just emits a small
fixed set of tasks covering every review branch.

Useful for: verifying OCR setup, checking the CLI, demonstrating the
needs_review flow, and CI.
"""

from __future__ import annotations

from typing import List

from ...models.task import RawExtractedTask
from .base import ExtractionContext, LLMExtractor


class StubExtractor(LLMExtractor):
    """Returns canned tasks that exercise each downstream branch."""

    name = "stub"

    def _extract_from_text(
        self, text: str, context: ExtractionContext
    ) -> List[RawExtractedTask]:
        # Only emit on the first chunk so multi-page documents don't multiply
        # the fixture.
        if context.chunk_index != 1:
            return []

        course = context.course_hint or "STUB 101"
        return [
            # Resolves cleanly -> syncable.
            RawExtractedTask(
                course_name=course,
                task_name="Problem Set 1",
                raw_date_expression="Week 3 Friday",
                grading_weight="10%",
                task_description="Stub task exercising the relative-week resolver.",
                source_page=context.page_range[0],
            ),
            # Resolves cleanly via the ordinal pattern -> syncable.
            RawExtractedTask(
                course_name=course,
                task_name="Midterm Exam",
                raw_date_expression="second Tuesday of October",
                grading_weight="30%",
                task_description="Stub task exercising the ordinal-weekday resolver.",
                source_page=context.page_range[0],
            ),
            # Unresolvable date -> flagged for review.
            RawExtractedTask(
                course_name=course,
                task_name="Final Project",
                raw_date_expression="TBD",
                grading_weight="40%",
                task_description="Stub task exercising the unresolvable-date branch.",
                source_page=context.page_range[0],
            ),
            # Contradiction -> flagged for review.
            RawExtractedTask(
                course_name=course,
                task_name="Lab Report 2",
                raw_date_expression="Oct 10",
                grading_weight="10%",
                task_description="Stub task exercising the contradiction branch.",
                contradiction_detected=True,
                contradiction_quotes=[
                    "Lab Report 2 is due Oct 10.",
                    "Schedule table: Lab Report 2 — Oct 17",
                ],
                source_page=context.page_range[0],
            ),
            # Missing grading weight -> flagged for review.
            RawExtractedTask(
                course_name=course,
                task_name="Participation",
                raw_date_expression="Week 14 Friday",
                grading_weight=None,
                task_description="Stub task exercising the missing-field branch.",
                source_page=context.page_range[0],
            ),
        ]
