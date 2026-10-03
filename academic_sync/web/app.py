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
import re
import secrets
import shutil
import tempfile
import threading
import uuid
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
    from starlette.concurrency import run_in_threadpool
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
from .auth import HostedAuth

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: Reject anything larger before it reaches the parser. A syllabus is a
#: handful of pages; a 50MB upload is a mistake or an attack, not a syllabus.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

TOKEN_HEADER = "x-app-token"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}

#: Job ids may be chosen by the browser (see ``Jobs.run``), so they are checked.
JOB_ID_RE = re.compile(r"^[a-z0-9]{8,32}$")

Work = Callable[[Callable[[str, str, str], None]], Dict[str, Any]]


class Jobs:
    """Long-running actions, tracked in the database.

    A serverless function has no memory between requests, so a poll that lands
    on a different instance than the request doing the work can only see the
    job if it lives in the database. Progress is therefore written there as
    the work goes.

    The **browser picks the job id** and sends it with the request. That lets
    it start polling at once, even when — hosted — the request itself stays
    open until the work finishes. A poll that arrives before the job row
    exists simply gets a 404 and tries again.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self._store.get_job(job_id)

    def launch(
        self,
        kind: str,
        label: str,
        work: Work,
        *,
        job_id: Optional[str] = None,
        cleanup: Optional[Callable[[], None]] = None,
        inline: bool = False,
    ) -> str:
        """Create a job and run ``work(progress)``.

        ``inline=False`` (local) runs it on a daemon thread and returns at
        once. ``inline=True`` (hosted) runs it in the calling thread, because
        a frozen serverless function would kill a background thread.
        """
        job_id = job_id or uuid.uuid4().hex[:12]
        if not JOB_ID_RE.match(job_id):
            raise HTTPException(400, "bad job id")
        if self._store.get_job(job_id) is not None:
            raise HTTPException(409, "that job id is already in use")
        self._store.create_job(job_id, kind, label)

        def run() -> None:
            self._store.update_job(job_id, status="running")

            def progress(stage: str, state: str, message: str) -> None:
                self._store.set_job_stage(job_id, stage, state, message)

            try:
                self._store.update_job(job_id, status="done", result=work(progress))
            except (PipelineError, WorkspaceError) as exc:
                # Expected, user-fixable failures (no API key, unreadable PDF,
                # no Google credentials). These messages were written to be
                # actionable, so they are surfaced verbatim.
                logger.info("job %s failed: %s", job_id, exc)
                self._store.update_job(job_id, status="error", error=str(exc))
            except Exception as exc:  # genuinely unexpected: log the trace, show the gist
                logger.exception("job %s crashed", job_id)
                self._store.update_job(job_id, status="error", error=f"unexpected error: {exc}")
            finally:
                if cleanup is not None:
                    cleanup()

        if inline:
            run()
        else:
            threading.Thread(target=run, daemon=True).start()
        return job_id


def create_app(
    paths: WorkspacePaths = WorkspacePaths(),
    *,
    workspace: Optional[Workspace] = None,
    hosted: Optional[HostedAuth] = None,
) -> Any:
    """Build the FastAPI application.

    Locally, call it with no arguments: a SQLite workspace at ``paths``, no
    login, a per-launch request token. Hosted, ``vercel_app`` passes a
    workspace backed by Postgres and a ``HostedAuth``, which replaces the
    token check with sign-in and a session-derived token.
    """
    app = FastAPI(title="Academic Sync Agent", docs_url=None if hosted else "/api/docs")
    workspace = workspace or Workspace(Store(paths.db), paths)
    jobs = Jobs(workspace.store)
    token = secrets.token_urlsafe(24)
    app.state.workspace = workspace
    app.state.token = token

    if hosted is not None:
        app.middleware("http")(hosted.guard)
        app.include_router(hosted.router())
    else:

        @app.middleware("http")
        async def require_token(request: Request, call_next: Any) -> Any:
            if request.method in ("POST", "PATCH", "PUT", "DELETE"):
                if not secrets.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
                    return JSONResponse({"detail": "missing or invalid app token"}, status_code=403)
            return await call_next(request)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def request_token(request: Request) -> str:
        if hosted is not None:
            return hosted.token_for(request.state.session)
        return token

    def launch(kind: str, label: str, work: Work, **kwargs: Any) -> Dict[str, Any]:
        # Hosted: run to completion inside this request (see Jobs.launch).
        job_id = jobs.launch(kind, label, work, inline=workspace.hosted, **kwargs)
        return {"job_id": job_id}

    @app.get("/")
    def index(request: Request) -> Any:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__APP_TOKEN__", request_token(request)))

    # -- reads ---------------------------------------------------------------

    @app.get("/api/state")
    def state(request: Request) -> Any:
        """Everything the shell needs on load: semester, tasks, connections."""
        data = _state(workspace)
        if hosted is not None:
            data["hosted"] = {"email": request.state.session["email"]}
            data["connections"]["google"]["client_configured"] = True
        return data

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> Any:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        return JSONResponse(job)

    @app.get("/api/demo-pdf")
    def demo_pdf() -> Any:
        """Serves the bundled sample syllabus, for a one-click demo import."""
        sample = Path(__file__).resolve().parents[2] / "examples" / "sample_syllabus_cs231.pdf"
        if not sample.is_file():
            raise HTTPException(404, "sample not bundled with this install")
        return FileResponse(sample, media_type="application/pdf", filename=sample.name)

    # -- settings ------------------------------------------------------------

    @app.post("/api/settings")
    def settings(request: Request, values: Dict[str, Any] = Body(...)) -> Any:
        _run(lambda: workspace.update_settings(values))
        return state(request)

    # -- imports -------------------------------------------------------------

    @app.post("/api/import/pdf")
    async def import_pdf(
        file: UploadFile = File(...),
        backend: Optional[str] = Form(None),
        model: Optional[str] = Form(None),
        job_id: Optional[str] = Form(None),
    ) -> Any:
        name = file.filename or "syllabus.pdf"
        if not name.lower().endswith(".pdf"):
            raise HTTPException(400, "please upload a PDF")
        _check_backend(backend)
        scratch = Path(tempfile.mkdtemp(prefix="acadsync-"))
        pdf_path = scratch / "upload.pdf"
        pdf_path.write_bytes(await _read_upload(file))
        return await run_in_threadpool(
            launch,
            "import",
            name,
            lambda progress: workspace.import_pdf(
                pdf_path, label=name, backend=backend, model=model, progress=progress
            ),
            job_id=job_id,
            cleanup=lambda: shutil.rmtree(scratch, ignore_errors=True),
        )

    @app.post("/api/import/images")
    async def import_images(
        files: List[UploadFile] = File(...),
        backend: Optional[str] = Form(None),
        model: Optional[str] = Form(None),
        job_id: Optional[str] = Form(None),
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
        return await run_in_threadpool(
            launch,
            "import",
            label,
            lambda progress: workspace.import_images(
                paths, label=label, backend=backend, model=model, progress=progress
            ),
            job_id=job_id,
            cleanup=lambda: shutil.rmtree(scratch, ignore_errors=True),
        )

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
        return launch(
            "import",
            "Gmail scan",
            lambda progress: workspace.import_gmail(
                query=query, max_results=max_results, backend=backend, progress=progress
            ),
            job_id=options.get("job_id"),
        )

    # -- sync & connections --------------------------------------------------

    @app.post("/api/sync")
    def sync(options: Dict[str, Any] = Body(...)) -> Any:
        targets = options.get("targets") or []
        task_ids = options.get("task_ids")
        return launch(
            "sync",
            "Sync",
            lambda progress: workspace.sync(targets, task_ids=task_ids, progress=progress),
            job_id=options.get("job_id"),
        )

    @app.post("/api/connections/google")
    def connect_google(options: Dict[str, Any] = Body(default={})) -> Any:
        if hosted is not None:
            # Consent happens in the browser, via the same sign-in round trip.
            return {"redirect": "/auth/login"}
        if not workspace.paths.credentials.exists():
            raise HTTPException(
                400,
                f"{workspace.paths.credentials} not found — download an OAuth Desktop "
                "client JSON from Google Cloud Console first (see the setup steps).",
            )
        return launch("connect", "Connect Google", workspace.connect_google, job_id=options.get("job_id"))

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
        workspace.disconnect_notion()
        return _state(workspace)

    # -- tasks ---------------------------------------------------------------

    @app.post("/api/tasks")
    def add_task(request: Request, fields: Dict[str, Any] = Body(...)) -> Any:
        _run(lambda: workspace.add_manual(fields))
        return state(request)

    @app.patch("/api/tasks/{task_id}")
    def fix_task(request: Request, task_id: int, changes: Dict[str, Any] = Body(...)) -> Any:
        _run(lambda: workspace.fix_task(task_id, changes))
        return state(request)

    @app.delete("/api/tasks/{task_id}")
    def dismiss_task(request: Request, task_id: int) -> Any:
        _run(lambda: workspace.dismiss(task_id))
        return state(request)

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
