"""The orchestrator: runs the five stages and owns the boundaries between them.

It is the only module that imports from more than one stage. Every stage is
reachable and testable without it.

What it is responsible for:

* running stages in order and converting each stage's failure into a clear,
  actionable message
* applying stage 3's verdict (a date, or an exception) to stage 4's model
* partitioning validated tasks into *syncable* and *needs review*
* writing ``extracted_tasks.json`` and ``needs_review.json``
* handing only syncable tasks to stage 5 (or nothing at all, on a dry run)

What it is deliberately *not* responsible for: knowing how PDFs are parsed,
which LLM is in use, how dates are computed, or how Google Calendar retries.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .config import PipelineConfig
from .extraction.llm import LLMExtractionError, create_extractor
from .extraction.pdf_extractor import PageText, PDFExtractionError, PDFTextExtractor
from .models.task import AcademicTask, RawExtractedTask
from .resolution import DateResolver, UnresolvableDateError

logger = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    """A stage failed in a way that stops the whole run."""


#: Stage identifiers, in execution order. Shared by the CLI and the web UI so
#: both describe the pipeline with the same vocabulary.
STAGES = ("extract_text", "extract_facts", "resolve_dates", "validate", "sync")

#: A callback invoked as each stage starts and finishes. Purely observational —
#: the pipeline's behaviour does not depend on it, and a callback that raises
#: must never take the run down with it (see ``_Progress.emit``).
ProgressCallback = Callable[[str, str, str], None]  # (stage, state, message)


@dataclass
class PipelineResult:
    """Everything the CLI needs to report and to choose an exit code."""

    all_tasks: List[AcademicTask] = field(default_factory=list)
    syncable: List[AcademicTask] = field(default_factory=list)
    needs_review: List[AcademicTask] = field(default_factory=list)
    ocr_pages: List[int] = field(default_factory=list)
    dry_run: bool = False
    sync_report: Optional[object] = None  # calendar_sync.SyncReport, lazily typed
    #: Wall-clock seconds per stage. Useful for the UI, and for noticing that
    #: stage 2 dominates the run (it does — everything else is milliseconds).
    stage_timings: Dict[str, float] = field(default_factory=dict)

    @property
    def interrupted(self) -> bool:
        return bool(getattr(self.sync_report, "interrupted", False))


class _Progress:
    """Times each stage and forwards state changes to an optional callback.

    Deliberately defensive: this exists to *report* on the pipeline, so a
    broken reporter (a closed websocket, a UI that went away) must not be able
    to fail a run that is otherwise succeeding.
    """

    def __init__(self, callback: Optional[ProgressCallback], timings: Dict[str, float]) -> None:
        self._callback = callback
        self._timings = timings

    @property
    def timings(self) -> Dict[str, float]:
        return self._timings

    def emit(self, stage: str, state: str, message: str = "") -> None:
        if self._callback is None:
            return
        try:
            self._callback(stage, state, message)
        except Exception:  # pragma: no cover - a reporter must never break a run
            logger.debug("progress callback raised; continuing", exc_info=True)

    @contextmanager
    def stage(self, stage: str, message: str = ""):
        self.emit(stage, "running", message)
        started = time.monotonic()
        try:
            yield
        except BaseException:
            self._timings[stage] = time.monotonic() - started
            self.emit(stage, "failed", "")
            raise
        self._timings[stage] = time.monotonic() - started


def run_pipeline(
    config: PipelineConfig, *, progress: Optional[ProgressCallback] = None
) -> PipelineResult:
    """Execute the full pipeline for one syllabus.

    Args:
        config: the run's configuration.
        progress: optional observer called as ``(stage, state, message)`` when
            each stage starts and finishes. Used by the web UI to show live
            progress; the CLI leaves it unset and nothing changes.
    """
    logger.info("=" * 70)
    logger.info("Syllabus:       %s", config.pdf_path)
    logger.info("Semester start: %s", config.semester_start_date.isoformat())
    logger.info("LLM backend:    %s", config.llm_backend)
    logger.info("Mode:           %s", "DRY RUN (no calendar writes)" if config.dry_run else "LIVE")
    logger.info("=" * 70)

    timings: Dict[str, float] = {}
    tracker = _Progress(progress, timings)

    with tracker.stage("extract_text", "reading the PDF"):
        document = _stage_1_extract_text(config)
    tracker.emit("extract_text", "done", f"{len(document.pages)} page(s)")

    result = _analyze(config, document.pages, source_name=config.pdf_path.name, tracker=tracker)
    _write_outputs(config, result)
    syncable = result.syncable

    if config.dry_run:
        logger.info("dry run: skipping Google Calendar entirely")
        _log_dry_run_plan(syncable)
        tracker.emit("sync", "skipped", "dry run — calendar untouched")
        return result

    with tracker.stage("sync", f"syncing to {config.calendar_id}"):
        result.sync_report = _stage_5_sync(config, syncable)
    tracker.emit("sync", "done", result.sync_report.summary())
    return result


def analyze_pages(
    config: PipelineConfig,
    pages: Sequence[PageText],
    *,
    source_name: str,
    progress: Optional[ProgressCallback] = None,
) -> PipelineResult:
    """Run stages 2–4 on text that came from somewhere other than a PDF.

    An email body, a screenshot's OCR output, or a pasted announcement all
    reduce to ``PageText``. From there the pipeline is identical: the LLM copies
    date phrases verbatim, the resolver computes them, and the review gate
    decides what is safe. Never syncs and never writes the JSON reports —
    callers that persist results own that.
    """
    return _analyze(
        config, pages, source_name=source_name, tracker=_Progress(progress, {})
    )


def _analyze(
    config: PipelineConfig,
    pages: Sequence[PageText],
    *,
    source_name: str,
    tracker: _Progress,
) -> PipelineResult:
    """Stages 2–4, shared by the PDF pipeline and :func:`analyze_pages`."""
    with tracker.stage("extract_facts", f"querying {config.llm_backend}"):
        raw_tasks = _stage_2_extract_facts(config, pages, source_name)
    tracker.emit("extract_facts", "done", f"{len(raw_tasks)} task(s) found")

    with tracker.stage("resolve_dates", "resolving dates deterministically"):
        tasks = _stage_3_and_4_resolve_and_validate(config, raw_tasks)
    resolved = sum(1 for t in tasks if t.exact_due_date is not None)
    tracker.emit("resolve_dates", "done", f"{resolved}/{len(tasks)} resolved")

    with tracker.stage("validate", "applying the review gate"):
        syncable = [t for t in tasks if t.is_syncable]
        needs_review = [t for t in tasks if t.requires_manual_review]
        result = PipelineResult(
            all_tasks=tasks,
            syncable=syncable,
            needs_review=needs_review,
            ocr_pages=[p.page_number for p in pages if p.source == "ocr"],
            dry_run=config.dry_run,
            stage_timings=tracker.timings,
        )
    tracker.emit("validate", "done", f"{len(syncable)} pass, {len(needs_review)} flagged")

    logger.info(
        "validation gate: %d syncable, %d flagged for manual review",
        len(syncable),
        len(needs_review),
    )
    return result


def build_resolver(config: PipelineConfig) -> DateResolver:
    """The stage-3 resolver for ``config``'s semester.

    Public so that a human correcting a flagged task goes through exactly the
    same resolver — with the same plausibility window — as the pipeline did.
    """
    return DateResolver(
        config.semester_start_date,
        week_start_weekday=config.week_start_weekday,
        grace_days_before=config.grace_days_before,
        max_horizon_days=config.max_horizon_days,
        day_first=config.day_first_dates,
    )


def resolve_raw_task(resolver: DateResolver, raw: RawExtractedTask) -> AcademicTask:
    """Resolve one raw task's date and build the validated model.

    The single-task form of stages 3+4, for manual entry and review fixes.
    """
    start, end, error = _resolve_one(resolver, raw)
    return AcademicTask.from_raw(
        raw, exact_due_date=start, end_date=end, date_resolution_error=error
    )


# ---------------------------------------------------------------------------
# Stage 1
# ---------------------------------------------------------------------------


def _stage_1_extract_text(config: PipelineConfig):
    logger.info("[1/5] extracting text from PDF")
    extractor = PDFTextExtractor(
        min_chars_per_page=config.min_chars_per_page,
        ocr_enabled=config.ocr_enabled,
        ocr_dpi=config.ocr_dpi,
        ocr_language=config.ocr_language,
        poppler_path=config.poppler_path,
        on_ocr_error=config.on_ocr_error,
    )
    try:
        document = extractor.extract(config.pdf_path)
    except PDFExtractionError as exc:
        raise PipelineError(f"stage 1 (PDF extraction) failed: {exc}") from exc

    logger.info(
        "[1/5] %d page(s); %d via OCR%s",
        len(document.pages),
        len(document.ocr_page_numbers),
        f" (pages {document.ocr_page_numbers})" if document.ocr_page_numbers else "",
    )
    if document.empty_page_numbers:
        # Not fatal, but the user should know a page contributed nothing —
        # a missing deadline may be hiding there.
        logger.warning(
            "no text recovered from page(s) %s; deadlines on them will be missed",
            document.empty_page_numbers,
        )
    return document


# ---------------------------------------------------------------------------
# Stage 2
# ---------------------------------------------------------------------------


def _stage_2_extract_facts(
    config: PipelineConfig, pages: Sequence[PageText], source_name: str
) -> List[RawExtractedTask]:
    logger.info("[2/5] semantic extraction via %r", config.llm_backend)
    try:
        extractor = create_extractor(
            config.llm_backend,
            model=config.llm_model,
            effort=config.llm_effort,
            max_chunk_chars=config.max_chunk_chars,
        )
    except (ValueError, LLMExtractionError) as exc:
        # An unknown backend name, a missing SDK, or an unusable credential —
        # all user-fixable configuration problems, so report them as a clean
        # message rather than a traceback.
        raise PipelineError(f"stage 2 (backend setup) failed: {exc}") from exc

    try:
        raw_tasks = extractor.extract(
            pages,
            source_name=source_name,
            course_hint=config.course_hint,
        )
    except LLMExtractionError as exc:
        # Fatal on purpose. "The model call failed" must never be reported as
        # "this syllabus has no deadlines".
        raise PipelineError(f"stage 2 (semantic extraction) failed: {exc}") from exc

    logger.info("[2/5] %d task(s) extracted", len(raw_tasks))
    if not raw_tasks:
        logger.warning(
            "the model found no graded tasks — check that the PDF is a syllabus "
            "and that OCR produced readable text (--verbose shows the extracted text volume)"
        )
    return raw_tasks


# ---------------------------------------------------------------------------
# Stages 3 + 4 (resolution feeds directly into validation)
# ---------------------------------------------------------------------------


def _stage_3_and_4_resolve_and_validate(
    config: PipelineConfig, raw_tasks: Sequence[RawExtractedTask]
) -> List[AcademicTask]:
    logger.info("[3/5] resolving dates (deterministic, no LLM)")
    resolver = build_resolver(config)

    tasks: List[AcademicTask] = []
    resolved_count = 0

    for raw in raw_tasks:
        start, end, error = _resolve_one(resolver, raw)
        if start is not None:
            resolved_count += 1
        # Construction runs the validator, which computes the review flags.
        tasks.append(
            AcademicTask.from_raw(
                raw,
                exact_due_date=start,
                end_date=end,
                date_resolution_error=error,
            )
        )

    logger.info(
        "[3/5] %d/%d date expression(s) resolved to an absolute date",
        resolved_count,
        len(raw_tasks),
    )
    logger.info("[4/5] validating and flagging")
    return tasks


def _resolve_one(
    resolver: DateResolver, raw: RawExtractedTask
) -> Tuple[Optional[date], Optional[date], Optional[str]]:
    """Resolve one expression into ``(start, end, None)`` or ``(None, None, reason)``.

    Uses the span-aware entry point so a genuine multi-day period ("End
    Semester Examinations, Dec 9 - Dec 23") survives as an event rather than
    being refused. ``end`` is ``None`` for an ordinary single-day deadline.

    The unresolvable path is a normal, expected outcome — not an error to be
    swallowed. The reason string travels into ``needs_review.json`` so a human
    can see exactly why the phrase defeated the resolver.
    """
    if not (raw.raw_date_expression or "").strip():
        return None, None, "the syllabus states no due date for this task"
    try:
        span = resolver.resolve_span(raw.raw_date_expression)
        return span.start, span.end, None
    except UnresolvableDateError as exc:
        logger.info(
            "unresolved: %r (%s / %s) — %s",
            raw.raw_date_expression,
            raw.course_name,
            raw.task_name,
            exc.reason,
        )
        return None, None, exc.reason


# ---------------------------------------------------------------------------
# Stage 5
# ---------------------------------------------------------------------------


def _stage_5_sync(config: PipelineConfig, syncable: Sequence[AcademicTask]):
    """Authenticate and sync. Imported lazily so dry runs need no Google libs."""
    logger.info("[5/5] syncing %d task(s) to calendar %s", len(syncable), config.calendar_id)

    from .calendar_sync.auth import build_calendar_service
    from .calendar_sync.errors import CalendarAuthError
    from .calendar_sync.google_calendar import CalendarSyncer
    from .calendar_sync.state import StateFileError, SyncState

    try:
        state = SyncState.load(config.state_path)
    except StateFileError as exc:
        raise PipelineError(str(exc)) from exc

    try:
        service = build_calendar_service(
            credentials_path=config.credentials_path,
            token_path=config.token_path,
            allow_interactive=config.allow_interactive_auth,
        )
    except CalendarAuthError as exc:
        raise PipelineError(f"stage 5 (authentication) failed: {exc}") from exc

    syncer = CalendarSyncer(
        service,
        calendar_id=config.calendar_id,
        state=state,
        reminder_minutes=config.reminder_minutes,
        max_attempts=config.max_sync_attempts,
    )
    report = syncer.sync_tasks(syncable)
    logger.info("[5/5] %s", report.summary())
    return report


# ---------------------------------------------------------------------------
# Output artefacts
# ---------------------------------------------------------------------------


def _write_outputs(config: PipelineConfig, result: PipelineResult) -> None:
    """Write ``extracted_tasks.json`` and ``needs_review.json``.

    The review file is written on every run — including when it is empty — so
    a stale file from a previous run can never be mistaken for the current one.
    """
    config.output_dir.mkdir(parents=True, exist_ok=True)

    _write_json(
        config.extracted_tasks_path,
        {
            "source_pdf": str(config.pdf_path),
            "semester_start_date": config.semester_start_date.isoformat(),
            "task_count": len(result.all_tasks),
            "tasks": [t.model_dump(mode="json") for t in result.all_tasks],
        },
    )

    _write_json(
        config.needs_review_path,
        {
            "source_pdf": str(config.pdf_path),
            "semester_start_date": config.semester_start_date.isoformat(),
            "review_count": len(result.needs_review),
            "note": (
                "These tasks were NOT synced to any calendar. Each has a "
                "review_reason explaining which gate it failed: missing required "
                "fields, a contradiction in the source text, or a date expression "
                "that could not be resolved."
            ),
            "tasks": [t.model_dump(mode="json") for t in result.needs_review],
        },
    )

    logger.info("wrote %s", config.extracted_tasks_path)
    logger.info("wrote %s (%d task(s))", config.needs_review_path, len(result.needs_review))


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _log_dry_run_plan(syncable: Sequence[AcademicTask]) -> None:
    """Show exactly what a live run would create."""
    if not syncable:
        logger.info("dry run: nothing would be synced")
        return
    logger.info("dry run: %d event(s) would be created:", len(syncable))
    for task in sorted(syncable, key=lambda t: t.exact_due_date or date.max):
        logger.info(
            "  %s  [%s] %s  (weight: %s, from %r)",
            task.exact_due_date.isoformat() if task.exact_due_date else "????-??-??",
            task.course_name,
            task.task_name,
            task.grading_weight,
            task.raw_date_expression,
        )
