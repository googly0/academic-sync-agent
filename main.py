#!/usr/bin/env python3
"""CLI entry point for the Autonomous Academic Management Agent.

    python main.py --pdf syllabus.pdf --semester-start 2026-01-12 --dry-run
    python main.py --pdf syllabus.pdf --semester-start 2026-01-12 \\
        --calendar-id primary

Exit codes (scriptable):

    0  everything synced (or a clean dry run) with nothing flagged
    1  completed, but some tasks need manual review
    2  the sync was interrupted — re-run to resume from the checkpoint
    3  a stage failed and the run could not complete
    4  bad arguments
  130  interrupted by the user (Ctrl-C)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from academic_sync.config import PipelineConfig, load_dotenv_if_present
from academic_sync.extraction.llm import available_backends
from academic_sync.logging_setup import configure_logging
from academic_sync.orchestrator import PipelineError, PipelineResult, run_pipeline

EXIT_OK = 0
EXIT_NEEDS_REVIEW = 1
EXIT_INTERRUPTED = 2
EXIT_FAILED = 3
EXIT_BAD_ARGS = 4
EXIT_USER_ABORT = 130

logger = logging.getLogger("academic_sync.cli")


def _iso_date(value: str) -> date:
    """argparse type for ``YYYY-MM-DD``.

    Strict on purpose: a mis-parsed semester start silently shifts every
    "Week N" deadline, which is the worst kind of bug this project can have.
    """
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an ISO date; expected YYYY-MM-DD (e.g. 2026-01-12)"
        ) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="academic-sync-agent",
        description=(
            "Extract assignment deadlines, grading weights, and exam dates from a "
            "course syllabus PDF and sync them to Google Calendar. Anything that "
            "cannot be resolved with certainty is flagged for review, never guessed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  Preview without touching the Calendar API:\n"
            "    python main.py --pdf syllabus.pdf --semester-start 2026-01-12 --dry-run\n\n"
            "  Sync for real:\n"
            "    python main.py --pdf syllabus.pdf --semester-start 2026-01-12 "
            "--calendar-id primary\n\n"
            "  Offline smoke test (no API key, no network):\n"
            "    python main.py --pdf syllabus.pdf --semester-start 2026-01-12 "
            "--llm-backend stub --dry-run\n"
        ),
    )

    core = parser.add_argument_group("core")
    core.add_argument("--pdf", required=True, type=Path, help="Path to the syllabus PDF.")
    core.add_argument(
        "--semester-start",
        required=True,
        type=_iso_date,
        metavar="YYYY-MM-DD",
        help="First day of the semester. Anchors all 'Week N' arithmetic and year inference.",
    )
    core.add_argument(
        "--calendar-id",
        default=None,
        help="Target Google Calendar id ('primary', or an address like "
        "abc123@group.calendar.google.com). Required unless --dry-run.",
    )
    core.add_argument(
        "--dry-run",
        action="store_true",
        help="Run stages 1-4 and print the plan. The Calendar API is never contacted "
        "and no credentials are needed.",
    )

    llm = parser.add_argument_group("semantic extraction (stage 2)")
    llm.add_argument(
        "--llm-backend",
        default="anthropic",
        choices=available_backends(),
        help="Which LLMExtractor implementation to use (default: anthropic).",
    )
    llm.add_argument("--model", default=None, help="Model id for the chosen backend.")
    llm.add_argument(
        "--effort",
        default=None,
        choices=["low", "medium", "high", "xhigh", "max"],
        help="Reasoning effort, where the backend supports it.",
    )
    llm.add_argument(
        "--chunk-chars",
        type=int,
        default=60_000,
        help="Soft max characters per model call (default: 60000).",
    )
    llm.add_argument(
        "--course",
        default=None,
        help="Optional course-name hint when the syllabus header is unclear or missing.",
    )

    pdf = parser.add_argument_group("PDF / OCR (stage 1)")
    pdf.add_argument(
        "--no-ocr",
        action="store_true",
        help="Never run OCR. Scanned pages come back empty and are reported.",
    )
    pdf.add_argument("--ocr-dpi", type=int, default=300, help="OCR rasterisation DPI (default: 300).")
    pdf.add_argument("--ocr-lang", default="eng", help="Tesseract language pack (default: eng).")
    pdf.add_argument(
        "--poppler-path", default=None, help="Explicit path to poppler binaries, if not on PATH."
    )
    pdf.add_argument(
        "--ocr-best-effort",
        action="store_true",
        help="Continue with an empty page when OCR is unavailable, instead of aborting.",
    )

    dates = parser.add_argument_group("date resolution (stage 3)")
    dates.add_argument(
        "--week-start",
        type=int,
        default=0,
        choices=range(0, 7),
        metavar="0-6",
        help="Weekday that begins an academic week (0=Monday .. 6=Sunday; default: 0).",
    )
    dates.add_argument(
        "--day-first",
        action="store_true",
        help="Read numeric dates as D/M instead of the default M/D.",
    )
    dates.add_argument(
        "--max-horizon-days",
        type=int,
        default=400,
        help="Reject dates more than this many days after the semester start (default: 400).",
    )

    sync = parser.add_argument_group("calendar sync (stage 5)")
    sync.add_argument(
        "--credentials", type=Path, default=Path("credentials.json"), help="OAuth client secrets."
    )
    sync.add_argument("--token", type=Path, default=Path("token.json"), help="Cached OAuth token.")
    sync.add_argument(
        "--state-file",
        type=Path,
        default=Path("sync_state.json"),
        help="Checkpoint file enabling resume-after-failure (default: sync_state.json).",
    )
    sync.add_argument(
        "--max-attempts",
        type=int,
        default=5,
        help="API attempts per call before backoff gives up (default: 5).",
    )
    sync.add_argument(
        "--non-interactive",
        action="store_true",
        help="Never open a browser for OAuth consent; fail fast instead. For servers/cron.",
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="Where extracted_tasks.json and needs_review.json are written (default: ./output).",
    )
    output.add_argument("--log-file", type=Path, default=None, help="Also write a debug log here.")
    output.add_argument("-v", "--verbose", action="store_true", help="Debug-level console logging.")

    return parser


def _config_from_args(args: argparse.Namespace) -> PipelineConfig:
    return PipelineConfig(
        pdf_path=args.pdf,
        semester_start_date=args.semester_start,
        ocr_enabled=not args.no_ocr,
        ocr_dpi=args.ocr_dpi,
        ocr_language=args.ocr_lang,
        poppler_path=args.poppler_path,
        on_ocr_error="warn" if args.ocr_best_effort else "raise",
        llm_backend=args.llm_backend,
        llm_model=args.model,
        llm_effort=args.effort,
        max_chunk_chars=args.chunk_chars,
        course_hint=args.course,
        week_start_weekday=args.week_start,
        max_horizon_days=args.max_horizon_days,
        day_first_dates=args.day_first,
        calendar_id=args.calendar_id or "primary",
        dry_run=args.dry_run,
        credentials_path=args.credentials,
        token_path=args.token,
        state_path=args.state_file,
        max_sync_attempts=args.max_attempts,
        allow_interactive_auth=not args.non_interactive,
        output_dir=args.output_dir,
    )


def _report(result: PipelineResult) -> None:
    """Human-readable summary on stdout (logs go to stderr, so this pipes cleanly)."""
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  tasks extracted:      {len(result.all_tasks)}")
    print(f"  ready to sync:        {len(result.syncable)}")
    print(f"  flagged for review:   {len(result.needs_review)}")
    if result.ocr_pages:
        print(f"  pages needing OCR:    {result.ocr_pages}")

    if result.needs_review:
        print()
        print("  Flagged (NOT synced) — see needs_review.json:")
        for task in result.needs_review:
            print(f"    - [{task.course_name or '?'}] {task.task_name or '?'}")
            print(f"        {task.review_reason}")

    report = result.sync_report
    if report is not None:
        print()
        print(f"  calendar sync:        {report.summary()}")
        if report.interrupted:
            print()
            print(f"  !! Sync stopped: {report.interruption_reason}")
            print(f"  !! {len(report.remaining)} task(s) not yet synced.")
            print("  !! Progress is checkpointed — re-run the same command to resume.")
    elif result.dry_run:
        print()
        print("  DRY RUN — no calendar events were created.")
    print("=" * 70)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # --calendar-id is only meaningful for a live run, but forgetting it there
    # would silently write to the user's primary calendar. Require it.
    if not args.dry_run and not args.calendar_id:
        parser.error("--calendar-id is required unless --dry-run is given")

    configure_logging(verbose=args.verbose, log_file=args.log_file)
    load_dotenv_if_present()

    if not args.pdf.is_file():
        logger.error("PDF not found: %s", args.pdf)
        return EXIT_BAD_ARGS

    config = _config_from_args(args)

    try:
        result = run_pipeline(config)
    except PipelineError as exc:
        logger.error("%s", exc)
        return EXIT_FAILED
    except KeyboardInterrupt:
        # The checkpoint is written per-event, so whatever was synced is safe.
        logger.warning("interrupted by user; any completed sync is checkpointed")
        return EXIT_USER_ABORT
    except Exception as exc:  # unexpected: show the traceback, it is a bug
        logger.exception("unexpected failure: %s", exc)
        return EXIT_FAILED

    _report(result)

    if result.interrupted:
        return EXIT_INTERRUPTED
    if result.needs_review:
        return EXIT_NEEDS_REVIEW
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
