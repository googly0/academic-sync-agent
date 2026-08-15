"""Run configuration.

One frozen dataclass carrying everything the orchestrator needs, so the
pipeline never reads ``os.environ`` or ``sys.argv`` from inside a stage. That
keeps every stage unit-testable without monkeypatching the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional, Tuple


def load_dotenv_if_present(path: str | Path = ".env") -> None:
    """Load a local ``.env`` when python-dotenv is installed.

    Optional by design: the project must run in an environment where secrets
    come from the real environment (CI, a container) with no ``.env`` at all.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = Path(path)
    if env_path.exists():
        load_dotenv(env_path)


@dataclass(frozen=True)
class PipelineConfig:
    """Everything one run needs. Constructed once, in the CLI."""

    # --- Input ------------------------------------------------------------
    pdf_path: Path
    semester_start_date: date

    # --- Stage 1: PDF / OCR ----------------------------------------------
    ocr_enabled: bool = True
    ocr_dpi: int = 300
    ocr_language: str = "eng"
    poppler_path: Optional[str] = None
    min_chars_per_page: int = 40
    on_ocr_error: str = "raise"

    # --- Stage 2: LLM -----------------------------------------------------
    llm_backend: str = "anthropic"
    llm_model: Optional[str] = None
    llm_effort: Optional[str] = None
    #: None defers to each backend's own default (see registry.py). Only a
    #: user-supplied --chunk-chars should ever set this to a concrete value —
    #: a hardcoded default here would silently override, e.g., Ollama's
    #: smaller default sized for a local context window.
    max_chunk_chars: Optional[int] = None
    course_hint: Optional[str] = None

    # --- Stage 3: date resolution ----------------------------------------
    week_start_weekday: int = 0  # Monday
    grace_days_before: int = 14
    max_horizon_days: int = 400
    day_first_dates: bool = False

    # --- Stage 5: calendar -----------------------------------------------
    calendar_id: str = "primary"
    dry_run: bool = False
    credentials_path: Path = Path("credentials.json")
    token_path: Path = Path("token.json")
    state_path: Path = Path("sync_state.json")
    max_sync_attempts: int = 5
    reminder_minutes: Tuple[int, ...] = (24 * 60, 60)
    allow_interactive_auth: bool = True

    # --- Output -----------------------------------------------------------
    output_dir: Path = field(default=Path("output"))

    @property
    def extracted_tasks_path(self) -> Path:
        return self.output_dir / "extracted_tasks.json"

    @property
    def needs_review_path(self) -> Path:
        return self.output_dir / "needs_review.json"

    @property
    def anthropic_api_key(self) -> Optional[str]:
        """Read at use-time, never stored on the dataclass.

        Keeping the key off the config object means a config dump in a log or
        an error report cannot leak it.
        """
        return os.environ.get("ANTHROPIC_API_KEY")
