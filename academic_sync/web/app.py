"""FastAPI server backing the local web app.

Design notes
------------
* **Writes to the outside world only happen on an explicit Sync.** Importing a
  PDF, screenshot, or email analyses and *stores* tasks; nothing reaches a
  calendar or Notion until the user presses Sync, which is the only endpoint
  that calls a syncer.
* **Mutating requests carry a per-process token.** The server binds to
  localhost, but any web page the user visits can still fire requests at
  ``localhost:8000``. Every POST/PATCH/DELETE must send ``X-App-Token``, which
  is injected into the page this server renders. A foreign page cannot read it,
  and the custom header forces a CORS preflight it will fail.
* **Long work runs on a background thread and is polled.** An LLM call or an
  OAuth consent can take a minute; polling was chosen over SSE deliberately —
  fewer failure modes, and a dropped connection just means the next poll picks
  the state back up.
* **Uploads are bounded and cleaned up.** Files go to a temp directory that is
  removed in a ``finally``, whether the import succeeded or not.

This module is the only place that knows about HTTP. ``Workspace`` does the
work and is tested without a server.
"""

from __future__ import annotations

import functools
import logging
import secrets
import shutil
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    # Imported at module scope, not inside create_app(): this module uses
    # `from __future__ import annotations`, so FastAPI's route annotations are
    # strings that Pydantic resolves against *module globals*. A function-local
    # import leaves `UploadFile` undefined at that point and every upload 500s.
    # The CLI never imports this module, so requiring FastAPI here costs it
    # nothing (see __init__.py, which loads create_app lazily).
    from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
except ImportError as exc:  # pragma: no cover - environment problem
    raise RuntimeError(
        "the web UI needs FastAPI and python-multipart: "
        "pip install -r requirements.txt"
    ) from exc

from ..extraction.image_extractor import ocr_available as _ocr_available
from ..extraction.llm import available_backends
from ..orchestrator import PipelineError
from ..sources.gmail import DEFAULT_QUERY
from ..store import Store
from ..workspace import Workspace, WorkspaceError, WorkspacePaths, default_backend

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: Reject anything larger before it reaches the parser. A syllabus is a
#: handful of pages; a 50MB upload is a mistake or an attack, not a syllabus.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

#: Finished jobs are kept so the browser can poll for the result, but not
#: forever — this is a local server, not a job queue.
MAX_RETAINED_JOBS = 40

TOKEN_HEADER = "x-app-token"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


@dataclass
class Job:
    """One long-running action, tracked across the polls that observe it."""

    id: str
    kind: str
    label: str
    status: str = "queued"  # queued | running | done | error
    stages: Dict[str, Dict[str, str]] = field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
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

    def create(self, kind: str, label: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label)
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

    def start(
        self,
        kind: str,
        label: str,
        work: Callable[[Callable[[str, str, str], None]], Dict[str, Any]],
        *,
        cleanup: Optional[Callable[[], None]] = None,
    ) -> Job:
        """Run ``work(progress)`` on a daemon thread and track it as a job."""
        job = self.create(kind, label)

        def run() -> None:
            self.update(job.id, status="running")

            def progress(stage: str, state: str, message: str) -> None:
                self.set_stage(job.id, stage, state, message)

            try:
                self.update(job.id, status="done", result=work(progress))
            except (PipelineError, WorkspaceError) as exc:
                # Expected, user-fixable failures (no API key, unreadable PDF,
                # no Google credentials). These messages were written to be
                # actionable, so they are surfaced verbatim.
                logger.info("job %s failed: %s", job.id, exc)
                self.update(job.id, status="error", error=str(exc))
            except Exception as exc:  # genuinely unexpected: log the trace, show the gist
                logger.exception("job %s crashed", job.id)
                self.update(job.id, status="error", error=f"unexpected error: {exc}")
            finally:
                if cleanup is not None:
                    cleanup()

        threading.Thread(target=run, daemon=True).start()
        return job


def create_app(paths: WorkspacePaths = WorkspacePaths()) -> Any:
    """Build the FastAPI application over the workspace at ``paths``."""
    app = FastAPI(title="Academic Sync Agent", docs_url="/api/docs")
    store = Store(paths.db)
    workspace = Workspace(store, paths)
    jobs = JobStore()
    token = secrets.token_urlsafe(24)
    app.state.workspace = workspace
    app.state.token = token

    @app.middleware("http")
    async def require_token(request: Request, call_next: Any) -> Any:
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            if not secrets.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
                return JSONResponse({"detail": "missing or invalid app token"}, status_code=403)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> Any:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__APP_TOKEN__", token))

    # -- reads ---------------------------------------------------------------

    @app.get("/api/state")
    def state() -> Any:
        """Everything the shell needs on load: semester, tasks, connections."""
        return _state(workspace)

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> Any:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        return JSONResponse(job.to_dict())

    @app.get("/api/demo-pdf")
    def demo_pdf() -> Any:
        """Serves the bundled sample syllabus, for a one-click demo import."""
        sample = Path(__file__).resolve().parents[2] / "examples" / "sample_syllabus_cs231.pdf"
        if not sample.is_file():
            raise HTTPException(404, "sample not bundled with this install")
        return FileResponse(sample, media_type="application/pdf", filename=sample.name)

    # -- settings ------------------------------------------------------------

    @app.post("/api/settings")
    def settings(values: Dict[str, Any] = Body(...)) -> Any:
        _run(lambda: workspace.update_settings(values))
        return _state(workspace)

    # -- imports -------------------------------------------------------------

    @app.post("/api/import/pdf")
    async def import_pdf(
        file: UploadFile = File(...),
        backend: Optional[str] = Form(None),
        model: Optional[str] = Form(None),
    ) -> Any:
        name = file.filename or "syllabus.pdf"
        if not name.lower().endswith(".pdf"):
            raise HTTPException(400, "please upload a PDF")
        _check_backend(backend)
        scratch = Path(tempfile.mkdtemp(prefix="acadsync-"))
        pdf_path = scratch / "upload.pdf"
        pdf_path.write_bytes(await _read_upload(file))
        job = jobs.start(
            "import",
            name,
            lambda progress: workspace.import_pdf(
                pdf_path, label=name, backend=backend, model=model, progress=progress
            ),
            cleanup=lambda: shutil.rmtree(scratch, ignore_errors=True),
        )
        return {"job_id": job.id}

    @app.post("/api/import/images")
    async def import_images(
        files: List[UploadFile] = File(...),
        backend: Optional[str] = Form(None),
        model: Optional[str] = Form(None),
    ) -> Any:
        _check_backend(backend)
        if not files:
            raise HTTPException(400, "choose at least one image")
        scratch = Path(tempfile.mkdtemp(prefix="acadsync-"))
        paths: List[Path] = []
        try:
            for n, upload in enumerate(files, start=1):
                suffix = Path(upload.filename or "").suffix.lower() or ".png"
                if suffix not in IMAGE_SUFFIXES:
                    raise HTTPException(400, f"{upload.filename}: not a supported image type")
                path = scratch / f"image-{n}{suffix}"
                path.write_bytes(await _read_upload(upload))
                paths.append(path)
        except BaseException:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
        label = files[0].filename or "Screenshot"
        if label in ("image.png", "blob"):  # pasted from the clipboard
            label = "Pasted screenshot"
        if len(files) > 1:
            label += f" (+{len(files) - 1} more)"
        job = jobs.start(
            "import",
            label,
            lambda progress: workspace.import_images(
                paths, label=label, backend=backend, model=model, progress=progress
            ),
            cleanup=lambda: shutil.rmtree(scratch, ignore_errors=True),
        )
        return {"job_id": job.id}

    @app.post("/api/import/gmail")
    def import_gmail(options: Dict[str, Any] = Body(default={})) -> Any:
        backend = options.get("backend") or None
        _check_backend(backend)
        query = (options.get("query") or "").strip() or None
        if query is not None:
            _run(lambda: workspace.update_settings({"gmail_query": query}))
        try:
            max_results = max(1, min(int(options.get("max_results") or 25), 100))
        except (TypeError, ValueError):
            raise HTTPException(400, "max_results must be a number") from None
        job = jobs.start(
            "import",
            "Gmail scan",
            lambda progress: workspace.import_gmail(
                query=query, max_results=max_results, backend=backend, progress=progress
            ),
        )
        return {"job_id": job.id}

    # -- sync & connections --------------------------------------------------

    @app.post("/api/sync")
    def sync(options: Dict[str, Any] = Body(...)) -> Any:
        targets = options.get("targets") or []
        task_ids = options.get("task_ids")
        job = jobs.start(
            "sync",
            "Sync",
            lambda progress: workspace.sync(targets, task_ids=task_ids, progress=progress),
        )
        return {"job_id": job.id}

    @app.post("/api/connections/google")
    def connect_google() -> Any:
        if not workspace.paths.credentials.exists():
            raise HTTPException(
                400,
                f"{workspace.paths.credentials} not found — download an OAuth Desktop "
                "client JSON from Google Cloud Console first (see the setup steps).",
            )
        job = jobs.start("connect", "Connect Google", workspace.connect_google)
        return {"job_id": job.id}

    @app.post("/api/connections/notion")
    def connect_notion(options: Dict[str, Any] = Body(...)) -> Any:
        info = _run(
            lambda: workspace.connect_notion(
                options.get("token"), options.get("database_id") or ""
            )
        )
        return {"database": info, **_state(workspace)}

    @app.delete("/api/connections/notion")
    def disconnect_notion() -> Any:
        workspace.store.set_settings({"notion_token": None, "notion_database_id": None})
        return _state(workspace)

    # -- tasks ---------------------------------------------------------------

    @app.post("/api/tasks")
    def add_task(fields: Dict[str, Any] = Body(...)) -> Any:
        _run(lambda: workspace.add_manual(fields))
        return _state(workspace)

    @app.patch("/api/tasks/{task_id}")
    def fix_task(task_id: int, changes: Dict[str, Any] = Body(...)) -> Any:
        _run(lambda: workspace.fix_task(task_id, changes))
        return _state(workspace)

    @app.delete("/api/tasks/{task_id}")
    def dismiss_task(task_id: int) -> Any:
        _run(lambda: workspace.dismiss(task_id))
        return _state(workspace)

    async def _read_upload(file: UploadFile) -> bytes:
        payload = await file.read()
        if not payload:
            raise HTTPException(400, f"{file.filename or 'that file'} is empty")
        if len(payload) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)}MB")
        return payload

    return app


def _check_backend(backend: Optional[str]) -> None:
    if backend and backend not in available_backends():
        raise HTTPException(400, f"unknown backend {backend!r}")


def _run(action: Callable[[], Any]) -> Any:
    """Run a quick workspace action, mapping user errors to a 400."""
    try:
        return action()
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc)) from None


def _state(workspace: Workspace) -> Dict[str, Any]:
    semester = workspace.semester()
    store = workspace.store
    return {
        "semester": (
            {
                "name": semester.name,
                "start": semester.start.isoformat(),
                "week_start": semester.week_start,
                "day_first": semester.day_first,
            }
            if semester
            else None
        ),
        "settings": {
            "llm_backend": store.get_setting("llm_backend") or default_backend(),
            "llm_model": store.get_setting("llm_model") or "",
            "calendar_id": store.get_setting("calendar_id") or "primary",
        },
        "backends": available_backends(),
        "gmail_query": store.get_setting("gmail_query") or DEFAULT_QUERY,
        "connections": {
            "google": workspace.google_status(),
            "notion": workspace.notion_status(),
            "ocr_problem": ocr_available(),
        },
        "sources": store.sources()[:30],
        "tasks": workspace.task_views(),
    }


@functools.lru_cache(maxsize=1)
def ocr_available() -> Optional[str]:
    """Cached: probing for tesseract spawns a process, and /api/state is hot."""
    return _ocr_available()


def main() -> None:
    """Entry point for ``python -m academic_sync.web``."""
    import argparse

    from ..config import load_dotenv_if_present
    from ..logging_setup import configure_logging

    parser = argparse.ArgumentParser(description="Run the local web app.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", type=Path, default=WorkspacePaths.db, help="workspace database file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    configure_logging(verbose=args.verbose)
    load_dotenv_if_present()

    try:
        import uvicorn
    except ImportError:
        raise SystemExit("the web UI needs uvicorn: pip install -r requirements.txt") from None

    print(f"\n  Academic Sync Agent — open http://{args.host}:{args.port}\n")
    uvicorn.run(create_app(WorkspacePaths(db=args.db)), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
