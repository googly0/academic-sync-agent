"""FastAPI server backing the local web UI.

Design notes
------------
* **Every analysis is a dry run.** The UI has no code path that writes to a
  calendar. Browsing to a page should never mutate anything outside a temp
  directory, so ``dry_run`` is hard-coded rather than exposed as a toggle.
* **Jobs run on a background thread and are polled.** The Ollama backend takes
  ~60s on a laptop; a synchronous request would look like a hung browser tab.
  Polling was chosen over SSE deliberately — fewer failure modes, and a
  dropped connection just means the next poll picks the state back up.
* **Uploads are bounded and cleaned up.** A PDF is written to a temp file,
  analysed, then deleted in a ``finally``, whether the run succeeded or not.

This module is intentionally the only place that knows about HTTP. The
pipeline itself is unchanged and unaware it is being driven by a browser.
"""

from __future__ import annotations

import logging
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    # Imported at module scope, not inside create_app(): this module uses
    # `from __future__ import annotations`, so FastAPI's route annotations are
    # strings that Pydantic resolves against *module globals*. A function-local
    # import leaves `UploadFile` undefined at that point and every upload 500s.
    # The CLI never imports this module, so requiring FastAPI here costs it
    # nothing (see __init__.py, which loads create_app lazily).
    from fastapi import FastAPI, File, Form, HTTPException, UploadFile
    from fastapi.responses import FileResponse, JSONResponse
except ImportError as exc:  # pragma: no cover - environment problem
    raise RuntimeError(
        "the web UI needs FastAPI and python-multipart: "
        "pip install -r requirements.txt"
    ) from exc

from ..config import PipelineConfig
from ..extraction.llm import available_backends
from ..models.task import AcademicTask
from ..orchestrator import STAGES, PipelineError, PipelineResult, run_pipeline

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: Reject anything larger before it reaches the parser. A syllabus is a
#: handful of pages; a 50MB upload is a mistake or an attack, not a syllabus.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

#: Finished jobs are kept so the browser can poll for the result, but not
#: forever — this is a local dev server, not a database.
MAX_RETAINED_JOBS = 40


@dataclass
class Job:
    """One analysis, tracked across the polling requests that observe it."""

    id: str
    filename: str
    status: str = "queued"  # queued | running | done | error
    stages: Dict[str, Dict[str, str]] = field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "status": self.status,
            "stages": self.stages,
            "result": self.result,
            "error": self.error,
        }


class JobStore:
    """In-memory job table with a lock, since a background thread writes to it
    while request handlers read from it."""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._lock = threading.Lock()

    def create(self, filename: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], filename=filename)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > MAX_RETAINED_JOBS:
                self._jobs.pop(self._order.pop(0), None)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)

    def set_stage(self, job_id: str, stage: str, state: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            # Replace the dict rather than mutating in place so a reader
            # holding the old reference always sees a consistent snapshot.
            stages = dict(job.stages)
            stages[stage] = {"state": state, "message": message}
            job.stages = stages


def _task_to_dict(task: AcademicTask) -> Dict[str, Any]:
    """Serialise a task for the UI.

    Includes ``raw_date_expression`` alongside ``exact_due_date`` on purpose:
    showing the syllabus's own wording next to the resolved date is what makes
    the deterministic resolver visible instead of magical.
    """
    return {
        "course_name": task.course_name,
        "task_name": task.task_name,
        "exact_due_date": task.exact_due_date.isoformat() if task.exact_due_date else None,
        "weekday": task.exact_due_date.strftime("%A") if task.exact_due_date else None,
        "grading_weight": task.grading_weight,
        "task_description": task.task_description,
        "raw_date_expression": task.raw_date_expression,
        "requires_manual_review": task.requires_manual_review,
        "review_reason": task.review_reason,
        "review_codes": _review_codes(task),
        "contradiction_detected": task.contradiction_detected,
        "contradiction_quotes": list(task.contradiction_quotes),
        "source_page": task.source_page,
    }


def _review_codes(task: AcademicTask) -> List[str]:
    """The machine-readable reasons a task was flagged.

    The UI groups and colours by these rather than pattern-matching the prose
    of ``review_reason``.
    """
    from ..models.task import (
        REVIEW_CONTRADICTION,
        REVIEW_MISSING_FIELDS,
        REVIEW_UNRESOLVED_DATE,
    )

    reason = task.review_reason or ""
    return [
        code
        for code in (REVIEW_CONTRADICTION, REVIEW_MISSING_FIELDS, REVIEW_UNRESOLVED_DATE)
        if code in reason
    ]


def _result_to_dict(result: PipelineResult, elapsed: float) -> Dict[str, Any]:
    syncable = [_task_to_dict(t) for t in result.syncable]
    syncable.sort(key=lambda t: t["exact_due_date"] or "9999-12-31")
    return {
        "summary": {
            "extracted": len(result.all_tasks),
            "syncable": len(result.syncable),
            "needs_review": len(result.needs_review),
            "ocr_pages": result.ocr_pages,
            "elapsed_seconds": round(elapsed, 2),
        },
        "stage_timings": {k: round(v, 3) for k, v in result.stage_timings.items()},
        "syncable": syncable,
        "needs_review": [_task_to_dict(t) for t in result.needs_review],
    }


def _run_job(store: JobStore, job_id: str, pdf_path: Path, config: PipelineConfig) -> None:
    """Execute one analysis on a worker thread."""
    import time

    store.update(job_id, status="running")
    started = time.monotonic()

    def on_progress(stage: str, state: str, message: str) -> None:
        store.set_stage(job_id, stage, state, message)

    try:
        result = run_pipeline(config, progress=on_progress)
        store.update(
            job_id,
            status="done",
            result=_result_to_dict(result, time.monotonic() - started),
        )
    except PipelineError as exc:
        # Expected, user-fixable failures (no API key, unreadable PDF, an
        # unreachable Ollama). Surfaced verbatim — these messages were written
        # to be actionable.
        logger.info("job %s failed: %s", job_id, exc)
        store.update(job_id, status="error", error=str(exc))
    except Exception as exc:  # genuinely unexpected: log the trace, show the gist
        logger.exception("job %s crashed", job_id)
        store.update(job_id, status="error", error=f"unexpected error: {exc}")
    finally:
        pdf_path.unlink(missing_ok=True)
        # The pipeline writes its JSON artefacts next to the temp PDF; clear
        # the whole scratch directory rather than leaving them behind.
        try:
            for leftover in config.output_dir.glob("*.json"):
                leftover.unlink(missing_ok=True)
            config.output_dir.rmdir()
        except OSError:
            pass


def create_app() -> Any:
    """Build the FastAPI application."""
    app = FastAPI(title="Academic Sync Agent", docs_url="/api/docs")
    store = JobStore()

    @app.get("/")
    def index() -> Any:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/demo-pdf")
    def demo_pdf() -> Any:
        """Serves the bundled sample syllabus.

        Lets the page offer a one-click demo for anyone who lands here without
        a syllabus of their own — and, usefully, makes the UI testable
        end-to-end without synthesising a file upload.
        """
        sample = Path(__file__).resolve().parents[2] / "examples" / "sample_syllabus_cs231.pdf"
        if not sample.is_file():
            raise HTTPException(404, "sample not bundled with this install")
        return FileResponse(sample, media_type="application/pdf", filename=sample.name)

    @app.get("/api/backends")
    def backends() -> Any:
        """Which extraction backends this install can actually use."""
        return {"backends": available_backends(), "default": "stub"}

    @app.post("/api/analyze")
    async def analyze(
        file: UploadFile = File(...),
        semester_start: str = Form(...),
        backend: str = Form("stub"),
        model: Optional[str] = Form(None),
    ) -> Any:
        if not (file.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "please upload a PDF")

        if backend not in available_backends():
            raise HTTPException(400, f"unknown backend {backend!r}")

        try:
            start_date = datetime.strptime(semester_start.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, "semester start must be YYYY-MM-DD") from None

        payload = await file.read()
        if not payload:
            raise HTTPException(400, "that file is empty")
        if len(payload) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                413, f"file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)}MB"
            )

        scratch = Path(tempfile.mkdtemp(prefix="acadsync-"))
        pdf_path = scratch / "upload.pdf"
        pdf_path.write_bytes(payload)

        config = PipelineConfig(
            pdf_path=pdf_path,
            semester_start_date=start_date,
            llm_backend=backend,
            llm_model=model or None,
            # Non-negotiable: a browser request must never touch a calendar.
            dry_run=True,
            output_dir=scratch,
            # OCR is best-effort here so a scanned page degrades to a warning
            # in the UI instead of failing the whole upload.
            on_ocr_error="warn",
        )

        job = store.create(file.filename or "syllabus.pdf")
        thread = threading.Thread(
            target=_run_job, args=(store, job.id, pdf_path, config), daemon=True
        )
        thread.start()
        return {"job_id": job.id}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> Any:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        return JSONResponse(job.to_dict())

    return app


def main() -> None:
    """Entry point for ``python -m academic_sync.web``."""
    import argparse

    from ..logging_setup import configure_logging

    parser = argparse.ArgumentParser(description="Run the local web UI.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    configure_logging(verbose=args.verbose)

    try:
        import uvicorn
    except ImportError:
        raise SystemExit(
            "the web UI needs uvicorn: pip install -r requirements.txt"
        ) from None

    print(f"\n  Academic Sync Agent — open http://{args.host}:{args.port}\n")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
