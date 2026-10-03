"""Push validated tasks into a Notion database, exactly once each.

Mirrors ``CalendarSyncer`` deliberately, so both targets behave the same way:

* **Idempotent.** Every page carries the task's ``sync_dedupe_key`` in a
  ``Sync Key`` property. Before creating a page the syncer queries for that
  key; if a page already exists it is *adopted*, not duplicated. That holds
  even if the app's database is deleted or the sync is run from another
  machine.
* **Retries only what can succeed on retry.** 429s honour ``Retry-After``;
  5xx get jittered exponential backoff; any other 4xx (bad token, database
  not shared with the integration) fails immediately — retrying it only
  burns time.
* **Stops cleanly.** An unrecoverable error ends the run with a report of
  what was confirmed and what remains, never a half-known state.
* **Never syncs a flagged task.** Checked here again as a hard invariant.

Uses the stable ``2022-06-28`` API version, where a database is queried
directly by its id.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..models.task import AcademicTask

logger = logging.getLogger(__name__)

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

PROP_COURSE = "Course"
PROP_DUE = "Due"
PROP_WEIGHT = "Weight"
PROP_WORDING = "Source wording"
PROP_KEY = "Sync Key"

#: Properties this syncer writes, and the Notion type each must have. The
#: title property is separate: every database has exactly one, under whatever
#: name the user gave it.
REQUIRED_PROPERTIES: Dict[str, str] = {
    PROP_COURSE: "select",
    PROP_DUE: "date",
    PROP_WEIGHT: "rich_text",
    PROP_WORDING: "rich_text",
    PROP_KEY: "rich_text",
}


class NotionSyncError(RuntimeError):
    """Base class for anything that goes wrong talking to Notion."""


class NotionAPIError(NotionSyncError):
    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class NotionSyncReport:
    created: List[str] = field(default_factory=list)
    adopted: List[str] = field(default_factory=list)
    already_synced: List[str] = field(default_factory=list)
    skipped_unsyncable: List[str] = field(default_factory=list)
    #: dedupe_key -> (page_id, url) for every task confirmed in this run.
    pages: Dict[str, Dict[str, str]] = field(default_factory=dict)
    interrupted: bool = False
    interruption_reason: Optional[str] = None
    remaining: List[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"created={len(self.created)}",
            f"adopted={len(self.adopted)}",
            f"already_synced={len(self.already_synced)}",
        ]
        if self.interrupted:
            parts.append(f"INTERRUPTED remaining={len(self.remaining)}")
        return " ".join(parts)


class NotionClient:
    """Minimal Notion REST client with retry. ``session`` is a requests-like
    object, injectable so tests never touch the network."""

    def __init__(
        self,
        token: str,
        *,
        session: Any = None,
        max_attempts: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token:
            raise NotionSyncError("no Notion token configured")
        if session is None:
            import requests

            session = requests.Session()
        self.session = session
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._sleep = sleep

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        last: Optional[NotionAPIError] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.request(
                    method, f"{API_BASE}{path}", headers=self.headers, json=body, timeout=30
                )
            except OSError as exc:  # network down, DNS, timeout
                last = NotionAPIError(f"{method} {path}: {exc}")
                retry_after = None
            else:
                if response.status_code < 400:
                    return response.json()
                last = NotionAPIError(
                    f"{method} {path} failed with HTTP {response.status_code}: "
                    f"{_message_of(response)}",
                    response.status_code,
                )
                if not _is_retryable(response.status_code):
                    raise last
                retry_after = _retry_after(response)

            if attempt == self.max_attempts:
                break
            delay = (
                min(retry_after, self.max_delay)
                if retry_after is not None
                else random.uniform(0, min(self.base_delay * 2 ** (attempt - 1), self.max_delay)) + 0.1
            )
            logger.warning("%s — retry %d/%d in %.1fs", last, attempt, self.max_attempts - 1, delay)
            self._sleep(delay)

        assert last is not None
        raise NotionAPIError(f"gave up after {self.max_attempts} attempts ({last})", last.status)


class NotionSyncer:
    def __init__(self, client: NotionClient, database_id: str) -> None:
        if not database_id:
            raise NotionSyncError("no Notion database configured")
        self.client = client
        self.database_id = _normalise_id(database_id)
        self._title_property: Optional[str] = None

    # -- schema --------------------------------------------------------------

    def ensure_schema(self) -> Dict[str, Any]:
        """Add any missing properties; refuse if one exists with the wrong type.

        Returns the database title for the UI to confirm the right one was
        picked.
        """
        database = self.client.request("GET", f"/databases/{self.database_id}")
        properties: Dict[str, Any] = database.get("properties") or {}

        titles = [name for name, prop in properties.items() if prop.get("type") == "title"]
        if not titles:  # pragma: no cover - Notion guarantees one
            raise NotionSyncError("that database has no title property")
        self._title_property = titles[0]

        missing: Dict[str, Any] = {}
        for name, kind in REQUIRED_PROPERTIES.items():
            existing = properties.get(name)
            if existing is None:
                missing[name] = {kind: {}}
            elif existing.get("type") != kind:
                raise NotionSyncError(
                    f"the database's {name!r} property is a {existing.get('type')}, "
                    f"but needs to be {kind}. Rename or delete it and try again."
                )
        if missing:
            logger.info("adding Notion properties: %s", ", ".join(missing))
            self.client.request(
                "PATCH", f"/databases/{self.database_id}", {"properties": missing}
            )

        title = "".join(t.get("plain_text", "") for t in database.get("title") or [])
        return {"title": title or "Untitled", "url": database.get("url"), "added": sorted(missing)}

    # -- sync ----------------------------------------------------------------

    def sync_tasks(
        self, tasks: Sequence[AcademicTask], *, known_keys: Sequence[str] = ()
    ) -> NotionSyncReport:
        """Sync every syncable task. Never raises for API failure mid-run.

        ``known_keys`` are dedupe keys already confirmed in this database (the
        app's own record), skipped with zero API calls — the checkpoint layer.
        """
        if self._title_property is None:
            self.ensure_schema()
        report = NotionSyncReport()
        known = set(known_keys)

        for index, task in enumerate(tasks):
            label = f"{task.course_name} / {task.task_name}"
            if not task.is_syncable:
                report.skipped_unsyncable.append(label)
                continue
            key = task.sync_dedupe_key
            assert key is not None
            if key in known:
                report.already_synced.append(label)
                continue
            try:
                existing = self._find(key)
                if existing is not None:
                    report.adopted.append(label)
                    report.pages[key] = _page_ref(existing)
                    continue
                page = self.client.request("POST", "/pages", self._page_body(task, key))
                report.created.append(label)
                report.pages[key] = _page_ref(page)
            except NotionAPIError as exc:
                report.interrupted = True
                report.interruption_reason = str(exc)
                report.remaining = [
                    f"{t.course_name} / {t.task_name}" for t in tasks[index:]
                ]
                logger.error("Notion sync interrupted at %s: %s", label, exc)
                break
        return report

    def _find(self, key: str) -> Optional[Dict[str, Any]]:
        response = self.client.request(
            "POST",
            f"/databases/{self.database_id}/query",
            {"filter": {"property": PROP_KEY, "rich_text": {"equals": key}}, "page_size": 1},
        )
        results = response.get("results") or []
        return results[0] if results else None

    def _page_body(self, task: AcademicTask, key: str) -> Dict[str, Any]:
        assert task.exact_due_date is not None and self._title_property is not None
        due: Dict[str, Any] = {"start": task.exact_due_date.isoformat()}
        if task.end_date:
            due["end"] = task.end_date.isoformat()
        properties: Dict[str, Any] = {
            self._title_property: {"title": _text(task.task_name or "")},
            PROP_COURSE: {"select": {"name": _select_name(task.course_name or "")}},
            PROP_DUE: {"date": due},
            PROP_WEIGHT: {"rich_text": _text(task.grading_weight or "")},
            PROP_WORDING: {"rich_text": _text(task.raw_date_expression or "")},
            PROP_KEY: {"rich_text": _text(key)},
        }
        body: Dict[str, Any] = {
            "parent": {"database_id": self.database_id},
            "properties": properties,
        }
        if task.task_description:
            body["children"] = [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": _text(task.task_description)},
                }
            ]
        return body


def _text(value: str) -> List[Dict[str, Any]]:
    # Notion caps a single rich-text item at 2000 characters.
    return [{"type": "text", "text": {"content": value[:2000]}}] if value else []


def _select_name(value: str) -> str:
    # Select option names may not contain commas, and are capped at 100 chars.
    return value.replace(",", " ").strip()[:100] or "Unknown course"


def _page_ref(page: Dict[str, Any]) -> Dict[str, str]:
    return {"id": page.get("id", ""), "url": page.get("url", "")}


def _normalise_id(value: str) -> str:
    """Accept a raw id, a dashed UUID, or a full notion.so URL."""
    value = value.strip().split("?")[0].rstrip("/")
    tail = value.rsplit("/", 1)[-1].rsplit("-", 1)[-1] if "notion.so" in value else value
    hex_id = tail.replace("-", "")
    if len(hex_id) == 32 and all(c in "0123456789abcdefABCDEF" for c in hex_id):
        return hex_id.lower()
    raise NotionSyncError(f"{value!r} does not look like a Notion database id or link")


def _is_retryable(status: int) -> bool:
    return status == 429 or status >= 500


def _retry_after(response: Any) -> Optional[float]:
    value = (getattr(response, "headers", None) or {}).get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _message_of(response: Any) -> str:
    try:
        return response.json().get("message", "") or response.text[:200]
    except Exception:
        return getattr(response, "text", "")[:200]
