"""The swappable LLM boundary.

``LLMExtractor`` is the only thing the orchestrator knows about. Swapping the
Anthropic backend for a local SLM (Ollama, a HF pipeline, llama.cpp, an
internal endpoint) means writing **one new subclass** that implements
``_extract_from_text`` and registering it — no orchestrator change, no
pipeline change, no changes to stages 1, 3, 4 or 5.

The base class deliberately owns the parts that are the *same* regardless of
backend:

* chunking a long syllabus into model-sized pieces
* iterating chunks and merging the results
* de-duplicating tasks that appear in two overlapping chunks
* normalising blank strings to ``None``

so a new backend is genuinely just "call my model, return
``List[RawExtractedTask]``".
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from ...models.task import RawExtractedTask
from ..pdf_extractor import PageText

logger = logging.getLogger(__name__)


class LLMExtractionError(RuntimeError):
    """The backend could not produce a usable structured result.

    Raised for refusals, truncated output, schema-invalid responses, and
    transport failures the backend chose not to retry. The orchestrator treats
    it as fatal for the run rather than silently returning zero tasks — "no
    deadlines found" and "the model call failed" must never look the same.
    """


@dataclass(frozen=True)
class ExtractionContext:
    """Everything a backend may need besides the text itself.

    Note what is *absent*: the semester start date. Stage 2 must not perform
    date arithmetic, so it is not given the information required to do so.
    """

    source_name: str
    chunk_index: int
    chunk_count: int
    page_range: Tuple[int, int]
    course_hint: Optional[str] = None


@dataclass(frozen=True)
class TextChunk:
    """A model-sized slice of the document, with its page span."""

    text: str
    page_range: Tuple[int, int]


class LLMExtractor(ABC):
    """Abstract base for every semantic-extraction backend.

    Subclasses implement exactly one method, ``_extract_from_text``.

    Args:
        max_chunk_chars: Soft ceiling on characters per model call. Chunks are
            split on page boundaries, so a single enormous page may exceed it;
            that is preferred to slicing a schedule table in half.
    """

    #: Registry key. Must be unique and stable — it is what ``--llm-backend``
    #: accepts on the command line.
    name: str = "abstract"

    def __init__(self, *, max_chunk_chars: int = 60_000) -> None:
        self.max_chunk_chars = max_chunk_chars

    # -- the one method a new backend must implement -----------------------

    @abstractmethod
    def _extract_from_text(
        self, text: str, context: ExtractionContext
    ) -> List[RawExtractedTask]:
        """Return the tasks stated in ``text``.

        Contract every implementation must honour:

        * Copy ``raw_date_expression`` **verbatim** from the source. Never
          normalise, never compute, never resolve. "Week 5 Friday" stays
          "Week 5 Friday".
        * Set ``contradiction_detected`` when the source states two different
          dates for the same task, and put both verbatim quotes in
          ``contradiction_quotes``. Do not decide which is right.
        * Leave a field ``None`` when the syllabus does not state it. Do not
          invent plausible values — stage 4 flags missing fields for a human,
          and that is the correct outcome.
        * Raise :class:`LLMExtractionError` on refusal, truncation, or an
          unparseable response. Never return ``[]`` to paper over a failure.
        """

    # -- shared pipeline plumbing ------------------------------------------

    def extract(
        self,
        pages: Sequence[PageText],
        *,
        source_name: str = "syllabus.pdf",
        course_hint: Optional[str] = None,
    ) -> List[RawExtractedTask]:
        """Extract tasks from a whole document, chunking as needed."""
        chunks = list(self._chunk(pages))
        if not chunks:
            raise LLMExtractionError(f"{source_name}: no text to extract from")

        logger.info(
            "semantic extraction: %d chunk(s) via backend %r", len(chunks), self.name
        )

        collected: List[RawExtractedTask] = []
        for index, chunk in enumerate(chunks, start=1):
            context = ExtractionContext(
                source_name=source_name,
                chunk_index=index,
                chunk_count=len(chunks),
                page_range=chunk.page_range,
                course_hint=course_hint,
            )
            tasks = self._extract_from_text(chunk.text, context)
            logger.info(
                "chunk %d/%d (pages %d-%d): %d task(s)",
                index,
                len(chunks),
                chunk.page_range[0],
                chunk.page_range[1],
                len(tasks),
            )
            collected.extend(self._postprocess(tasks, chunk))

        return _deduplicate(collected)

    def _chunk(self, pages: Sequence[PageText]) -> Iterable[TextChunk]:
        """Group pages into chunks under ``max_chunk_chars``.

        Splitting only ever happens at a page boundary. A schedule table split
        mid-row would strand dates from their assignments, which is precisely
        the failure this pipeline is built to avoid.
        """
        buffer: List[str] = []
        buffer_chars = 0
        first_page: Optional[int] = None
        last_page: Optional[int] = None

        for page in pages:
            body = page.text.strip()
            if not body:
                continue
            block = f"--- PAGE {page.page_number} ---\n{body}"

            if buffer and buffer_chars + len(block) > self.max_chunk_chars:
                yield TextChunk("\n\n".join(buffer), (first_page or 1, last_page or 1))
                buffer, buffer_chars, first_page = [], 0, None

            buffer.append(block)
            buffer_chars += len(block)
            first_page = page.page_number if first_page is None else first_page
            last_page = page.page_number

        if buffer:
            yield TextChunk("\n\n".join(buffer), (first_page or 1, last_page or 1))

    def _postprocess(
        self, tasks: Sequence[RawExtractedTask], chunk: TextChunk
    ) -> List[RawExtractedTask]:
        """Normalise blanks to ``None`` and backfill the page number.

        Models sometimes return ``""`` where they mean "not stated"; collapsing
        that here means stage 4's missing-field check does not have to know
        about model quirks.
        """
        cleaned: List[RawExtractedTask] = []
        for task in tasks:
            data = task.model_dump()
            for key in (
                "course_name",
                "task_name",
                "raw_date_expression",
                "grading_weight",
                "task_description",
            ):
                value = data.get(key)
                data[key] = value.strip() if isinstance(value, str) and value.strip() else None
            if data.get("source_page") is None:
                data["source_page"] = chunk.page_range[0]
            # Quotes are meaningless without the flag, and vice versa.
            if not data.get("contradiction_detected"):
                data["contradiction_quotes"] = []
            cleaned.append(RawExtractedTask(**data))
        return cleaned


def _deduplicate(tasks: Sequence[RawExtractedTask]) -> List[RawExtractedTask]:
    """Drop exact repeats of (course, task, date phrase).

    Only *identical* triples are merged. Two entries that agree on course and
    task but differ on date phrase are kept separately on purpose — that is a
    contradiction the review step should surface, not something to quietly
    collapse here.
    """
    seen = set()
    unique: List[RawExtractedTask] = []
    for task in tasks:
        key = (
            (task.course_name or "").strip().lower(),
            (task.task_name or "").strip().lower(),
            (task.raw_date_expression or "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(task)
    return unique
