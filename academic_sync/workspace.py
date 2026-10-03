"""The web app's service layer: imports, human fixes, and sync, over a Store.

Every input — a PDF, a screenshot, an email, a hand-typed task — is reduced to
the same thing before it is stored: an ``AcademicTask`` that went through the
deterministic resolver and the review gate. Nothing here computes a date by
any other route, so the pipeline's guarantees hold for the whole app, not just
the CLI.

This module knows nothing about HTTP; ``web/app.py`` is a thin layer over it.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .config import PipelineConfig
from .models.task import (
    REVIEW_CONTRADICTION,
    REVIEW_MISSING_FIELDS,
    REVIEW_UNRESOLVED_DATE,
    AcademicTask,
    RawExtractedTask,
)
from .orchestrator import (
    PipelineError,
    ProgressCallback,
    analyze_pages,
    build_resolver,
    resolve_raw_task,
    run_pipeline,
)
from .store import STATUS_ACTIVE, Store, StoredTask

logger = logging.getLogger(__name__)

TARGET_GCAL = "gcal"
TARGET_NOTION = "notion"
TARGETS = (TARGET_GCAL, TARGET_NOTION)


class WorkspaceError(RuntimeError):
    """A user-fixable problem, reported verbatim in the UI."""


@dataclass(frozen=True)
class WorkspacePaths:
    """Where the app keeps its files. Defaults match the CLI's."""

    db: Path = Path("academic_sync.db")
    credentials: Path = Path("credentials.json")
    token: Path = Path("token.json")
    state: Path = Path("sync_state.json")


@dataclass(frozen=True)
class Semester:
    name: str
    start: date
    week_start: int = 0
    day_first: bool = False

    def week_of(self, day: date) -> int:
        """1-based academic week containing ``day`` (Week 1 contains ``start``)."""
        week1 = self.start - timedelta(days=(self.start.weekday() - self.week_start) % 7)
        return (day - week1).days // 7 + 1


class Workspace:
    def __init__(
        self,
        store: Store,
        paths: WorkspacePaths = WorkspacePaths(),
        *,
        hosted: bool = False,
        token_store: Any = None,
        box: Any = None,
    ) -> None:
        """
        Args:
            hosted: running on a serverless host. The Calendar checkpoint then
                lives in the database (there is no disk), and imports do not
                spawn threads.
            token_store: where Google's token lives; ``None`` means
                ``paths.token`` on disk, as locally.
            box: a ``SecretsBox``. When given, the Notion token is stored
                encrypted in the database rather than as a plain setting.
        """
        self.store = store
        self.paths = paths
        self.hosted = hosted
        self.token_store = token_store
        self.box = box

    # -- settings ----------------------------------------------------------

    def semester(self) -> Optional[Semester]:
        start = self.store.get_setting("semester_start")
        if not start:
            return None
        return Semester(
            name=self.store.get_setting("semester_name") or "My semester",
            start=date.fromisoformat(start),
            week_start=int(self.store.get_setting("week_start") or 0),
            day_first=self.store.get_setting("day_first") == "1",
        )

    def update_settings(self, values: Dict[str, Any]) -> None:
        """Validate and persist settings sent from the UI."""
        clean: Dict[str, Optional[str]] = {}
        for key, value in values.items():
            if key == "semester_start":
                try:
                    clean[key] = date.fromisoformat(str(value)).isoformat()
                except ValueError:
                    raise WorkspaceError("semester start must be YYYY-MM-DD") from None
            elif key == "week_start":
                if str(value) not in {str(i) for i in range(7)}:
                    raise WorkspaceError("week start must be 0 (Mon) to 6 (Sun)")
                clean[key] = str(value)
            elif key == "day_first":
                clean[key] = "1" if value in (True, "1", "true", "on") else "0"
            elif key == "notion_token":
                self._set_notion_token((str(value).strip() if value is not None else "") or None)
            elif key in SETTING_KEYS:
                text = (str(value).strip() if value is not None else "") or None
                clean[key] = text
            else:
                raise WorkspaceError(f"unknown setting {key!r}")
        self.store.set_settings(clean)

    def _require_semester(self) -> Semester:
        semester = self.semester()
        if semester is None:
            raise WorkspaceError(
                "set your semester start date first — every 'Week N' is counted from it"
            )
        return semester

    def pipeline_config(
        self,
        *,
        pdf_path: Optional[Path] = None,
        backend: Optional[str] = None,
        model: Optional[str] = None,
        output_dir: Optional[Path] = None,
    ) -> PipelineConfig:
        semester = self._require_semester()
        return PipelineConfig(
            pdf_path=pdf_path,
            semester_start_date=semester.start,
            week_start_weekday=semester.week_start,
            day_first_dates=semester.day_first,
            llm_backend=backend or self.store.get_setting("llm_backend") or default_backend(),
            llm_model=model or self.store.get_setting("llm_model") or None,
            # Analysis never syncs. Sync is a separate, explicit action.
            dry_run=True,
            output_dir=output_dir or Path(tempfile.gettempdir()),
            # A bad scan degrades to a warning in the UI rather than failing
            # the whole import. Hosted there is no Tesseract, so scanned PDF
            # pages are skipped (and reported) rather than OCR'd.
            ocr_enabled=not self.hosted,
            on_ocr_error="warn",
            calendar_id=self.store.get_setting("calendar_id") or "primary",
            credentials_path=self.paths.credentials,
            token_path=self.paths.token,
            state_path=self.paths.state,
        )

    # -- imports -----------------------------------------------------------

    def import_pdf(
        self,
        pdf_path: Path,
        *,
        label: str,
        backend: Optional[str] = None,
        model: Optional[str] = None,
        progress: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        scratch = Path(tempfile.mkdtemp(prefix="acadsync-"))
        try:
            config = self.pipeline_config(
                pdf_path=pdf_path, backend=backend, model=model, output_dir=scratch
            )
            result = run_pipeline(config, progress=progress)
        except PipelineError as exc:
            if self.hosted and "no text recovered" in str(exc):
                raise WorkspaceError(
                    "that PDF has no text layer (it looks scanned). The hosted app "
                    "can't OCR PDFs — upload photos or screenshots of its pages instead."
                ) from exc
            raise
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        source_id = self.store.add_source("pdf", label)
        return {**self._store_result(result.all_tasks, source_id), "empty_pages": result.empty_pages}

    def import_images(
        self,
        image_paths: Sequence[Path],
        *,
        label: str,
        backend: Optional[str] = None,
        model: Optional[str] = None,
        progress: Optional[ProgressCallback] = None,
        image_extractor: Any = None,
    ) -> Dict[str, Any]:
        """Screenshots or photos: read the text, then the same stages 2–4 as a PDF."""
        from .extraction.pdf_extractor import PDFExtractionError

        config = self.pipeline_config(backend=backend, model=model)
        _emit(progress, "extract_text", "running", f"reading {len(image_paths)} image(s)")
        try:
            extractor = image_extractor or self._image_extractor(config)
            document = extractor.extract(image_paths)
        except PDFExtractionError as exc:
            _emit(progress, "extract_text", "failed", "")
            raise PipelineError(f"could not read the image: {exc}") from exc
        if not any(p.text.strip() for p in document.pages):
            _emit(progress, "extract_text", "failed", "")
            raise WorkspaceError("no readable text found in that image")
        _emit(progress, "extract_text", "done", f"{len(document.pages)} image(s) read")

        result = analyze_pages(config, document.pages, source_name=label, progress=progress)
        source_id = self.store.add_source("image", label)
        return self._store_result(result.all_tasks, source_id)

    def import_gmail(
        self,
        *,
        query: Optional[str] = None,
        max_results: int = 25,
        backend: Optional[str] = None,
        model: Optional[str] = None,
        progress: Optional[ProgressCallback] = None,
        gmail: Any = None,
    ) -> Dict[str, Any]:
        """Scan recent course emails; each new one is analysed on its own.

        One model call per email rather than one for the batch, so every task
        stays attributed to the message it came from. Every scanned message is
        recorded — including ones with no deadlines — so a rescan skips it.
        """
        from .sources.gmail import GmailSource, parse_query

        config = self.pipeline_config(backend=backend, model=model)
        if gmail is None:
            gmail = GmailSource(self._gmail_service())
        query = parse_query(query or self.store.get_setting("gmail_query"))

        _emit(progress, "extract_text", "running", "searching Gmail")
        ids = gmail.list_ids(query, max_results=max_results)
        new_ids = [i for i in ids if not self.store.has_source("gmail", i)]
        _emit(
            progress,
            "extract_text",
            "done",
            f"{len(ids)} matching, {len(new_ids)} new",
        )

        totals = {"found": 0, "added": 0, "duplicates": 0, "flagged": 0}
        emails: List[Dict[str, Any]] = []
        for n, message_id in enumerate(new_ids, start=1):
            email = gmail.fetch(message_id)
            _emit(progress, "extract_facts", "running", f"email {n}/{len(new_ids)}: {email.label}")
            result = analyze_pages(config, [email.to_page()], source_name=f"email: {email.label}")
            source_id = self.store.add_source(
                "gmail",
                email.label,
                external_id=message_id,
                meta={"from": email.sender, "date": email.date},
            )
            counts = self._store_result(result.all_tasks, source_id)
            for key in totals:
                totals[key] += counts[key]
            emails.append({"subject": email.label, "from": email.sender, "found": counts["found"]})
        _emit(progress, "extract_facts", "done", f"{len(new_ids)} email(s) read")
        _emit(progress, "validate", "done", f"{totals['added']} new task(s)")
        return {**totals, "emails": emails, "scanned": len(ids), "new_emails": len(new_ids)}

    # -- image reading ---------------------------------------------------

    def image_reader(self) -> str:
        """``"vision"`` (Claude reads the image) or ``"tesseract"`` (local OCR).

        Hosted there are no native binaries, so it is always vision. Locally it
        is Tesseract unless ``IMAGE_READER=vision`` asks otherwise.
        """
        if self.hosted or os_env("IMAGE_READER") == "vision":
            return "vision"
        return "tesseract"

    def image_reader_problem(self) -> Optional[str]:
        """``None`` when screenshots can be read, else why not. For the UI."""
        if self.image_reader() == "vision":
            return None if os_env("ANTHROPIC_API_KEY") else "ANTHROPIC_API_KEY is not set"
        from .extraction.image_extractor import ocr_available

        return ocr_available()

    def _image_extractor(self, config: PipelineConfig) -> Any:
        from .extraction.image_extractor import ImageTextExtractor, VisionImageExtractor

        if self.image_reader() == "vision":
            return VisionImageExtractor()
        return ImageTextExtractor(ocr_language=config.ocr_language)

    def add_manual(self, fields: Dict[str, Any]) -> StoredTask:
        """A task typed in by hand. Its date still goes through the resolver."""
        semester = self._require_semester()
        raw = RawExtractedTask(
            course_name=_clean(fields.get("course_name")),
            task_name=_clean(fields.get("task_name")),
            raw_date_expression=_clean(fields.get("date_phrase")),
            grading_weight=_clean(fields.get("grading_weight")),
            task_description=_clean(fields.get("task_description")),
        )
        task = resolve_raw_task(self._resolver(semester), raw)
        source_id = self.store.add_source("manual", "Added by hand")
        counts = self.store.add_tasks([task], source_id=source_id)
        if not counts["added"]:
            raise WorkspaceError("that task is already in your semester")
        return next(t for t in reversed(self.store.tasks()) if t.source_id == source_id)

    def _store_result(self, tasks: Sequence[AcademicTask], source_id: int) -> Dict[str, Any]:
        counts = self.store.add_tasks(tasks, source_id=source_id)
        return {
            **counts,
            "found": len(tasks),
            "flagged": sum(1 for t in tasks if t.requires_manual_review),
        }

    # -- review & fix ------------------------------------------------------

    def fix_task(self, task_id: int, changes: Dict[str, Any]) -> StoredTask:
        """Apply a human correction and send the task back through the gate.

        ``changes`` may carry:

        * ``course_name`` / ``task_name`` / ``grading_weight`` /
          ``task_description`` — plain edits.
        * ``date_phrase`` — new wording, resolved by the same deterministic
          resolver the pipeline uses. Any contradiction flag stays put: new
          wording is not a ruling on which of two quoted dates is right.
        * ``date`` (+ optional ``end_date``), ISO — a date the human picked.
          That *is* a ruling, so it also clears a contradiction. The source's
          own wording is kept for provenance.

        The result is rebuilt as an ``AcademicTask``, so the validator decides
        whether it can sync. An edit cannot route around the review gate.
        """
        stored = self.store.get_task(task_id)
        if stored is None:
            raise WorkspaceError("no such task")
        semester = self._require_semester()

        fields = stored.task.model_dump(
            exclude={"sync_dedupe_key", "requires_manual_review", "review_reason"}
        )
        for key in ("course_name", "task_name", "grading_weight", "task_description"):
            if key in changes:
                fields[key] = _clean(changes[key])

        if changes.get("date"):
            start = _parse_iso(changes["date"], "date")
            end = _parse_iso(changes["end_date"], "end date") if changes.get("end_date") else None
            if end is not None and end < start:
                raise WorkspaceError("the end date is before the start date")
            fields.update(
                exact_due_date=start,
                end_date=end if end != start else None,
                date_resolution_error=None,
                contradiction_detected=False,
            )
        elif "date_phrase" in changes:
            raw = RawExtractedTask(
                course_name=fields["course_name"],
                task_name=fields["task_name"],
                raw_date_expression=_clean(changes["date_phrase"]),
            )
            resolved = resolve_raw_task(self._resolver(semester), raw)
            fields.update(
                raw_date_expression=resolved.raw_date_expression,
                exact_due_date=resolved.exact_due_date,
                end_date=resolved.end_date,
                date_resolution_error=resolved.date_resolution_error,
            )

        return self.store.replace_task(task_id, AcademicTask(**fields))

    def dismiss(self, task_id: int) -> None:
        if not self.store.dismiss_task(task_id):
            raise WorkspaceError("no such task")

    def _resolver(self, semester: Semester):
        return build_resolver(
            PipelineConfig(
                pdf_path=None,
                semester_start_date=semester.start,
                week_start_weekday=semester.week_start,
                day_first_dates=semester.day_first,
            )
        )

    # -- connections -------------------------------------------------------

    def google_status(self) -> Dict[str, Any]:
        from .calendar_sync.auth import google_status

        return google_status(
            credentials_path=self.paths.credentials,
            token_path=self.paths.token,
            token_store=self.token_store,
        )

    def connect_google(self, progress: Optional[ProgressCallback] = None) -> Dict[str, Any]:
        """Run the browser consent for Calendar + Gmail. Blocks until done."""
        from .calendar_sync.auth import APP_SCOPES, load_google_credentials
        from .calendar_sync.errors import CalendarAuthError

        _emit(progress, "connect", "running", "waiting for you in the Google consent tab")
        try:
            load_google_credentials(
                APP_SCOPES,
                credentials_path=self.paths.credentials,
                token_path=self.paths.token,
                allow_interactive=True,
                token_store=self.token_store,
            )
        except CalendarAuthError as exc:
            raise WorkspaceError(str(exc)) from exc
        _emit(progress, "connect", "done", "connected")
        return self.google_status()

    def _gmail_service(self) -> Any:
        from .calendar_sync.auth import build_gmail_service
        from .calendar_sync.errors import CalendarAuthError

        try:
            return build_gmail_service(
                credentials_path=self.paths.credentials,
                token_path=self.paths.token,
                allow_interactive=False,
                token_store=self.token_store,
            )
        except CalendarAuthError as exc:
            raise WorkspaceError(str(exc)) from exc

    def _get_notion_token(self) -> Optional[str]:
        if self.box is not None:
            ciphertext = self.store.get_secret("notion_token")
            return self.box.decrypt(ciphertext) if ciphertext else None
        return self.store.get_setting("notion_token")

    def _set_notion_token(self, token: Optional[str]) -> None:
        if self.box is None:
            self.store.set_settings({"notion_token": token})
        elif token is None:
            self.store.delete_secret("notion_token")
        else:
            self.store.put_secret("notion_token", self.box.encrypt(token))

    def disconnect_notion(self) -> None:
        self._set_notion_token(None)
        self.store.set_settings({"notion_database_id": None})

    def notion_settings(self) -> Dict[str, Optional[str]]:
        import os

        return {
            "token": self._get_notion_token() or os.environ.get("NOTION_TOKEN"),
            "database_id": self.store.get_setting("notion_database_id")
            or os.environ.get("NOTION_DATABASE_ID"),
        }

    def connect_notion(
        self, token: Optional[str], database_id: str, *, client: Any = None
    ) -> Dict[str, Any]:
        """Check the token and database, add missing properties, then save."""
        from .notion_sync import NotionClient, NotionSyncer, NotionSyncError

        token = _clean(token) or self.notion_settings()["token"]
        try:
            client = client or NotionClient(token or "")
            syncer = NotionSyncer(client, database_id or "")
            info = syncer.ensure_schema()
        except NotionSyncError as exc:
            raise WorkspaceError(_notion_hint(exc)) from exc
        self.store.set_settings({"notion_database_id": syncer.database_id})
        if token and token != os_env("NOTION_TOKEN"):
            self._set_notion_token(token)
        return info

    def notion_status(self) -> Dict[str, Any]:
        settings = self.notion_settings()
        return {
            "token_configured": bool(settings["token"]),
            "database_id": settings["database_id"],
            "connected": bool(settings["token"] and settings["database_id"]),
        }

    # -- sync --------------------------------------------------------------

    def sync(
        self,
        targets: Sequence[str],
        *,
        task_ids: Optional[Sequence[int]] = None,
        progress: Optional[ProgressCallback] = None,
        calendar_service: Any = None,
        notion_client: Any = None,
    ) -> Dict[str, Any]:
        """Push every active task to each target. The only external write.

        Only tasks that passed the review gate (status ``active``) are sent,
        and each syncer re-checks that as an invariant. Both targets are
        idempotent, so syncing twice — or after a crash — creates nothing new.
        """
        unknown = set(targets) - set(TARGETS)
        if unknown or not targets:
            raise WorkspaceError(f"choose sync targets from {', '.join(TARGETS)}")
        wanted = set(task_ids) if task_ids is not None else None
        tasks = [
            t
            for t in self.store.tasks()
            if t.status == STATUS_ACTIVE and (wanted is None or t.id in wanted)
        ]
        out: Dict[str, Any] = {}
        for target in targets:
            if target == TARGET_GCAL:
                out[target] = self._sync_gcal(tasks, progress, calendar_service)
            else:
                out[target] = self._sync_notion(tasks, progress, notion_client)
        return out

    def _sync_gcal(
        self, tasks: List[StoredTask], progress: Optional[ProgressCallback], service: Any
    ) -> Dict[str, Any]:
        from .calendar_sync.auth import build_calendar_service
        from .calendar_sync.errors import CalendarAuthError
        from .calendar_sync.google_calendar import CalendarSyncer
        from .calendar_sync.state import DbSyncState, StateFileError, SyncState

        calendar_id = self.target_scope(TARGET_GCAL)
        _emit(progress, "sync_gcal", "running", f"syncing {len(tasks)} task(s) to Google Calendar")
        try:
            # Hosted there is no disk, so the checkpoint is a database table.
            state = DbSyncState(self.store) if self.hosted else SyncState.load(self.paths.state)
            if service is None:
                service = build_calendar_service(
                    credentials_path=self.paths.credentials,
                    token_path=self.paths.token,
                    allow_interactive=False,
                    token_store=self.token_store,
                )
        except (StateFileError, CalendarAuthError) as exc:
            _emit(progress, "sync_gcal", "failed", "")
            raise WorkspaceError(str(exc)) from exc

        report = CalendarSyncer(service, calendar_id=calendar_id, state=state).sync_tasks(
            [t.task for t in tasks]
        )
        for stored in tasks:
            key = stored.task.sync_dedupe_key
            record = state.get(key, calendar_id) if key else None
            scoped = f"{calendar_id}:{key}"
            if record is not None:
                due = stored.task.exact_due_date
                self.store.record_sync(
                    stored.id,
                    TARGET_GCAL,
                    dedupe_key=scoped,
                    remote_id=record.event_id,
                    remote_url=(
                        f"https://calendar.google.com/calendar/r/day/{due:%Y/%m/%d}" if due else None
                    ),
                )
            elif report.interrupted:
                self.store.record_sync(
                    stored.id, TARGET_GCAL, dedupe_key=scoped, error=report.interruption_reason
                )
        _emit(
            progress,
            "sync_gcal",
            "failed" if report.interrupted else "done",
            report.summary(),
        )
        return {
            "created": len(report.created),
            "adopted": len(report.adopted),
            "already_synced": len(report.already_synced),
            "interrupted": report.interrupted,
            "error": report.interruption_reason,
        }

    def _sync_notion(
        self, tasks: List[StoredTask], progress: Optional[ProgressCallback], client: Any
    ) -> Dict[str, Any]:
        from .notion_sync import NotionClient, NotionSyncer, NotionSyncError

        settings = self.notion_settings()
        _emit(progress, "sync_notion", "running", f"syncing {len(tasks)} task(s) to Notion")
        try:
            client = client or NotionClient(settings["token"] or "")
            syncer = NotionSyncer(client, settings["database_id"] or "")
            syncer.ensure_schema()
        except NotionSyncError as exc:
            _emit(progress, "sync_notion", "failed", "")
            raise WorkspaceError(_notion_hint(exc)) from exc

        scope = syncer.database_id
        syncs = self.store.syncs_for(t.id for t in tasks)
        known = [
            rec["dedupe_key"].split(":", 1)[1]
            for per_task in syncs.values()
            for target, rec in per_task.items()
            if target == TARGET_NOTION
            and rec.get("synced_at")
            and (rec.get("dedupe_key") or "").startswith(scope + ":")
        ]
        report = syncer.sync_tasks([t.task for t in tasks], known_keys=known)
        for stored in tasks:
            key = stored.task.sync_dedupe_key
            scoped = f"{scope}:{key}"
            page = report.pages.get(key or "")
            if page is not None:
                self.store.record_sync(
                    stored.id,
                    TARGET_NOTION,
                    dedupe_key=scoped,
                    remote_id=page["id"],
                    remote_url=page["url"],
                )
            elif report.interrupted and key not in known:
                self.store.record_sync(
                    stored.id, TARGET_NOTION, dedupe_key=scoped, error=report.interruption_reason
                )
        _emit(
            progress,
            "sync_notion",
            "failed" if report.interrupted else "done",
            report.summary(),
        )
        return {
            "created": len(report.created),
            "adopted": len(report.adopted),
            "already_synced": len(report.already_synced),
            "interrupted": report.interrupted,
            "error": report.interruption_reason,
        }

    def target_scope(self, target: str) -> Optional[str]:
        """Which calendar / database a target currently points at.

        Sync records are keyed by scope + dedupe key, so pointing the app at a
        different calendar correctly shows every task as not yet synced there.
        """
        if target == TARGET_GCAL:
            return self.store.get_setting("calendar_id") or "primary"
        database_id = self.notion_settings()["database_id"]
        if not database_id:
            return None
        from .notion_sync.notion import _normalise_id

        try:
            return _normalise_id(database_id)
        except Exception:
            return None

    # -- views -------------------------------------------------------------

    def task_views(self) -> List[Dict[str, Any]]:
        stored = self.store.tasks()
        syncs = self.store.syncs_for(t.id for t in stored)
        sources = {s["id"]: s for s in self.store.sources()}
        semester = self.semester()
        scopes = {target: self.target_scope(target) for target in TARGETS}
        return [
            task_to_dict(t, syncs.get(t.id, {}), sources.get(t.source_id), semester, scopes)
            for t in stored
        ]


SETTING_KEYS = {
    "semester_name",
    "semester_start",
    "week_start",
    "day_first",
    "llm_backend",
    "llm_model",
    "calendar_id",
    "gmail_query",
    "notion_token",
    "notion_database_id",
}


def task_to_dict(
    stored: StoredTask,
    syncs: Dict[str, Dict[str, Any]],
    source: Optional[Dict[str, Any]],
    semester: Optional[Semester],
    scopes: Dict[str, Optional[str]],
) -> Dict[str, Any]:
    """Serialise a stored task for the UI.

    ``raw_date_expression`` travels next to the resolved date on purpose: the
    source's own wording beside the computed date is what makes the resolver
    checkable rather than magical.
    """
    task = stored.task
    due = task.exact_due_date
    key = task.sync_dedupe_key
    return {
        "id": stored.id,
        "status": stored.status,
        "course_name": task.course_name,
        "task_name": task.task_name,
        "exact_due_date": due.isoformat() if due else None,
        "weekday": due.strftime("%A") if due else None,
        "end_date": task.end_date.isoformat() if task.end_date else None,
        "week": semester.week_of(due) if (semester and due) else None,
        "grading_weight": task.grading_weight,
        "task_description": task.task_description,
        "raw_date_expression": task.raw_date_expression,
        "review_reason": task.review_reason,
        "review_codes": [
            code
            for code in (REVIEW_UNRESOLVED_DATE, REVIEW_CONTRADICTION, REVIEW_MISSING_FIELDS)
            if code in (task.review_reason or "")
        ],
        "contradiction_quotes": list(task.contradiction_quotes),
        "source_page": task.source_page,
        "source": (
            {"kind": source["kind"], "label": source["label"], "meta": source["meta"]}
            if source
            else None
        ),
        "sync": {
            target: _sync_state(syncs.get(target), f"{scopes.get(target)}:{key}")
            for target in TARGETS
        },
    }


def _sync_state(record: Optional[Dict[str, Any]], key: str) -> Dict[str, Any]:
    """One target's status: synced, changed (synced under an older date or a
    different calendar/database), error, pending.

    ``key`` is ``"<scope>:<dedupe key>"`` — what a record must match to count
    as synced *to where the app currently points*.
    """
    if record is None:
        return {"state": "pending"}
    if record.get("synced_at") and record.get("dedupe_key") == key:
        return {"state": "synced", "url": record.get("remote_url"), "at": record["synced_at"]}
    if record.get("last_error"):
        return {"state": "error", "error": record["last_error"]}
    if record.get("synced_at"):
        # The task's date or name changed after it was synced. The old event
        # is left alone (never auto-deleted); the next sync adds the new one.
        return {"state": "changed", "url": record.get("remote_url")}
    return {"state": "pending"}


def default_backend() -> str:
    """Anthropic when a key is configured, else the offline stub, so the app
    works out of the box and never silently calls a paid API by surprise."""
    return "anthropic" if os_env("ANTHROPIC_API_KEY") else "stub"


def os_env(name: str) -> Optional[str]:
    import os

    return os.environ.get(name)


def _notion_hint(exc: Exception) -> str:
    """Turn the two most common Notion failures into a next step."""
    message = str(exc)
    status = getattr(exc, "status", None)
    if status == 401:
        return "Notion rejected the token — copy the integration secret again (it starts with 'ntn_' or 'secret_')."
    if status == 404:
        return (
            "Notion can't see that database. Open it in Notion → ••• → Connections, "
            "and add your integration."
        )
    return message


def _emit(progress: Optional[ProgressCallback], stage: str, state: str, message: str) -> None:
    if progress is not None:
        try:
            progress(stage, state, message)
        except Exception:  # pragma: no cover - a reporter must never break a run
            logger.debug("progress callback raised; continuing", exc_info=True)


def _clean(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None


def _parse_iso(value: Any, what: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise WorkspaceError(f"{what} must be YYYY-MM-DD") from None
