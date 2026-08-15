"""Stage 5 — idempotent, rate-limit-aware, resumable Google Calendar sync.

Three guarantees, each implemented explicitly rather than hoped for:

**Idempotency.** Every event carries its ``sync_dedupe_key`` as a private
extended property. Before creating anything we check two places in order:
the local checkpoint (free), then the calendar itself via a
``privateExtendedProperty`` query (one cheap API call). A re-run after a lost
state file therefore adopts the existing events instead of duplicating them.

**Backoff.** 429s and 403-quota errors are retried with exponential backoff
plus jitter, honouring ``Retry-After`` when the server sends it. Non-retryable
4xx errors (bad calendar id, revoked scope) fail immediately — retrying them
burns quota and delays the report.

**Resumability.** The checkpoint is written after every confirmed event. When
retries are exhausted or the network drops, the run *stops cleanly*: state is
already durable, a report describes exactly where it stopped, and the next
invocation skips everything already recorded.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, TypeVar

from googleapiclient.errors import HttpError

from ..models.task import AcademicTask
from .errors import NonRetryableAPIError, RateLimitExhaustedError
from .state import SyncState

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Extended-property key holding our dedupe key on each event.
DEDUPE_PROPERTY = "academic_sync_key"

#: HTTP statuses always worth retrying — transient by definition.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

#: 403 is overloaded by Google: quota problems are transient, permission
#: problems are not. Only these reason codes get retried.
RETRYABLE_403_REASONS = frozenset(
    {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "quotaExceeded",
        "backendError",
    }
)


@dataclass
class SyncReport:
    """Outcome of one sync run — the CLI's exit code is derived from this."""

    created: List[str] = field(default_factory=list)
    already_synced: List[str] = field(default_factory=list)  # known from checkpoint
    adopted: List[str] = field(default_factory=list)  # found on calendar, not in state
    skipped_unsyncable: List[str] = field(default_factory=list)
    interrupted: bool = False
    interruption_reason: Optional[str] = None
    remaining: List[str] = field(default_factory=list)

    @property
    def total_confirmed(self) -> int:
        return len(self.created) + len(self.already_synced) + len(self.adopted)

    def summary(self) -> str:
        parts = [
            f"created={len(self.created)}",
            f"already_synced={len(self.already_synced)}",
            f"adopted={len(self.adopted)}",
            f"skipped={len(self.skipped_unsyncable)}",
        ]
        if self.interrupted:
            parts.append(f"INTERRUPTED remaining={len(self.remaining)}")
        return " ".join(parts)


class CalendarSyncer:
    """Pushes validated tasks onto a Google Calendar, exactly once each.

    Args:
        service: An authorised Calendar v3 service (see ``auth.py``).
        calendar_id: Target calendar. ``"primary"`` is the signed-in user's.
        state: The checkpoint. Mutated and saved as the run progresses.
        reminder_minutes: Popup reminder lead times on created events.
        max_attempts: Attempts per API call before giving up (1 = no retry).
        base_delay / max_delay: Exponential-backoff bounds in seconds.
    """

    def __init__(
        self,
        service: Any,
        *,
        calendar_id: str,
        state: SyncState,
        reminder_minutes: Sequence[int] = (24 * 60, 60),
        max_attempts: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 64.0,
    ) -> None:
        self.service = service
        self.calendar_id = calendar_id
        self.state = state
        self.reminder_minutes = tuple(reminder_minutes)
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay

    # -- public API --------------------------------------------------------

    def sync_tasks(self, tasks: Sequence[AcademicTask]) -> SyncReport:
        """Sync every syncable task, stopping cleanly on unrecoverable errors.

        Never raises for API failure. The failure is captured in the returned
        report so the caller can log it, set an exit code, and tell the user to
        re-run — which is exactly what resumability is for.
        """
        report = SyncReport()
        pending = list(tasks)

        for index, task in enumerate(pending):
            label = _label(task)

            if not task.is_syncable:
                # Should already have been filtered out upstream; treated as a
                # hard invariant here so a bug can never leak a flagged task
                # onto a calendar.
                logger.debug("skipping %s: not syncable", label)
                report.skipped_unsyncable.append(label)
                continue

            key = task.sync_dedupe_key
            assert key is not None  # guaranteed by is_syncable

            if self.state.is_synced(key, self.calendar_id):
                logger.info("skip (checkpoint): %s", label)
                report.already_synced.append(label)
                continue

            try:
                existing = self._find_existing_event(key)
                if existing is not None:
                    # The calendar has it but our state file didn't: adopt it.
                    # This is the path that saves a user who deleted
                    # sync_state.json from a calendar full of duplicates.
                    logger.info("adopt (already on calendar): %s", label)
                    self._record(task, key, existing["id"])
                    report.adopted.append(label)
                    continue

                event = self._create_event(task, key)
                self._record(task, key, event["id"])
                report.created.append(label)
                logger.info("created: %s -> %s", label, event.get("htmlLink", event["id"]))

            except (RateLimitExhaustedError, NonRetryableAPIError, OSError) as exc:
                # Stop cleanly. State for everything before this point is
                # already durable (``_record`` saves immediately).
                report.interrupted = True
                report.interruption_reason = str(exc)
                report.remaining = [_label(t) for t in pending[index:]]
                logger.error(
                    "sync interrupted at %s: %s — %d task(s) remaining; "
                    "re-run to resume from the checkpoint",
                    label,
                    exc,
                    len(report.remaining),
                )
                break

        # Belt and braces: ``_record`` already saved, but a run that created
        # nothing should still leave a valid file on disk.
        self.state.save()
        return report

    # -- event construction ------------------------------------------------

    def _build_event_body(self, task: AcademicTask, dedupe_key: str) -> Dict[str, Any]:
        """All-day event on the due date.

        All-day rather than timed because a syllabus almost never states a
        meaningful time, and inventing "23:59 in some timezone" is the same
        class of fabrication this pipeline refuses elsewhere. Google's all-day
        ``end.date`` is exclusive, hence the +1 day.
        """
        assert task.exact_due_date is not None
        due = task.exact_due_date

        description_lines = [
            task.task_description or "",
            "",
            f"Course: {task.course_name}",
            f"Weight: {task.grading_weight}",
            f"Syllabus wording: {task.raw_date_expression!r}",
        ]
        if task.source_page:
            description_lines.append(f"Source: syllabus page {task.source_page}")
        description_lines += [
            "",
            "Created by academic-sync-agent. Editing the title or date here will "
            "not be reflected back into the syllabus, and a future run will not "
            "overwrite your edits.",
        ]

        return {
            "summary": f"[{task.course_name}] {task.task_name}",
            "description": "\n".join(line for line in description_lines).strip(),
            "start": {"date": due.isoformat()},
            "end": {"date": (due + timedelta(days=1)).isoformat()},
            "transparency": "transparent",  # a deadline should not show as busy
            # The idempotency anchor. Private properties are invisible to
            # attendees and survive user edits to the title/description.
            "extendedProperties": {"private": {DEDUPE_PROPERTY: dedupe_key}},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": m} for m in self.reminder_minutes
                ],
            },
        }

    # -- API calls (each wrapped in backoff) -------------------------------

    def _find_existing_event(self, dedupe_key: str) -> Optional[Dict[str, Any]]:
        """Look the event up by its private extended property.

        Server-side filtering, so this is one small request regardless of how
        many events the calendar holds.
        """
        response = self._with_backoff(
            lambda: self.service.events()
            .list(
                calendarId=self.calendar_id,
                privateExtendedProperty=f"{DEDUPE_PROPERTY}={dedupe_key}",
                maxResults=1,
                singleEvents=True,
                showDeleted=False,
            )
            .execute(),
            description=f"lookup {dedupe_key}",
        )
        items = response.get("items") or []
        return items[0] if items else None

    def _create_event(self, task: AcademicTask, dedupe_key: str) -> Dict[str, Any]:
        body = self._build_event_body(task, dedupe_key)
        return self._with_backoff(
            lambda: self.service.events()
            .insert(calendarId=self.calendar_id, body=body)
            .execute(),
            description=f"insert {dedupe_key}",
        )

    def _record(self, task: AcademicTask, dedupe_key: str, event_id: str) -> None:
        """Checkpoint immediately after the API confirms an event.

        Saving per-event rather than per-batch is the whole point: a crash one
        event later must not lose the record of this one.
        """
        self.state.mark_synced(
            dedupe_key,
            self.calendar_id,
            event_id=event_id,
            course_name=task.course_name,
            task_name=task.task_name,
            due_date=task.exact_due_date.isoformat() if task.exact_due_date else None,
        )
        self.state.save()

    # -- retry machinery ---------------------------------------------------

    def _with_backoff(self, call: Callable[[], T], *, description: str) -> T:
        """Run ``call``, retrying transient failures with jittered backoff.

        Raises:
            RateLimitExhaustedError: attempts exhausted on a retryable error.
            NonRetryableAPIError: the error will fail identically on retry.
        """
        last_error: Optional[HttpError] = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                return call()
            except HttpError as exc:
                last_error = exc
                status = _status_of(exc)
                reason = _reason_of(exc)

                if not _is_retryable(status, reason):
                    raise NonRetryableAPIError(
                        f"{description} failed with HTTP {status}"
                        + (f" ({reason})" if reason else "")
                        + f": {_message_of(exc)}"
                    ) from exc

                if attempt == self.max_attempts:
                    break

                delay = self._delay_for(attempt, exc)
                logger.warning(
                    "%s: HTTP %s%s — retry %d/%d in %.1fs",
                    description,
                    status,
                    f" ({reason})" if reason else "",
                    attempt,
                    self.max_attempts - 1,
                    delay,
                )
                time.sleep(delay)

        raise RateLimitExhaustedError(
            f"{description}: gave up after {self.max_attempts} attempts "
            f"(last error: HTTP {_status_of(last_error)} {_message_of(last_error)}). "
            "Progress has been checkpointed; re-run to resume."
        )

    def _delay_for(self, attempt: int, exc: HttpError) -> float:
        """Exponential backoff with full jitter, honouring ``Retry-After``.

        Jitter matters: without it, a batch that trips a rate limit retries in
        lockstep and trips it again at exactly the same moment.
        """
        retry_after = _retry_after_of(exc)
        if retry_after is not None:
            return min(retry_after, self.max_delay)
        ceiling = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        return random.uniform(0.0, ceiling) + 0.1


# ---------------------------------------------------------------------------
# HttpError introspection — defensive because the shape varies by API/version
# ---------------------------------------------------------------------------


def _status_of(exc: Optional[HttpError]) -> Optional[int]:
    if exc is None:
        return None
    return getattr(getattr(exc, "resp", None), "status", None)


def _message_of(exc: Optional[HttpError]) -> str:
    if exc is None:
        return "unknown error"
    payload = _payload_of(exc)
    return (payload.get("message") if isinstance(payload, dict) else None) or str(exc)


def _reason_of(exc: HttpError) -> Optional[str]:
    payload = _payload_of(exc)
    if not isinstance(payload, dict):
        return None
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return errors[0].get("reason")
    return payload.get("status")


def _payload_of(exc: HttpError) -> Dict[str, Any]:
    """Best-effort decode of the JSON error body; ``{}`` when unavailable."""
    content = getattr(exc, "content", None)
    if not content:
        return {}
    try:
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        body = json.loads(content)
    except (ValueError, AttributeError):
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def _retry_after_of(exc: HttpError) -> Optional[float]:
    """Read the ``Retry-After`` header, if the server sent a usable one.

    ``httplib2.Response`` is a ``dict`` subclass whose keys are lower-cased
    header names, but other transports expose headers as attributes or on a
    ``.headers`` mapping — so all three are checked rather than assumed.
    """
    resp = getattr(exc, "resp", None)
    if resp is None:
        return None

    value = None
    for source in (resp, getattr(resp, "headers", None), getattr(resp, "__dict__", None)):
        if not isinstance(source, dict):
            continue
        value = source.get("retry-after") or source.get("Retry-After")
        if value is not None:
            break

    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        # Retry-After may also be an HTTP date. Rather than parse it, fall
        # back to exponential backoff — never worse than not retrying.
        return None


def _is_retryable(status: Optional[int], reason: Optional[str]) -> bool:
    if status in RETRYABLE_STATUSES:
        return True
    # 403 means "quota" sometimes and "you don't have permission" other times.
    # Only the former is worth retrying.
    if status == 403 and reason in RETRYABLE_403_REASONS:
        return True
    return False


def _label(task: AcademicTask) -> str:
    return f"{task.course_name or '?'} / {task.task_name or '?'}"
