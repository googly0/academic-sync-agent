"""The checkpoint file — what makes a failed run resumable instead of fatal.

``sync_state.json`` records which dedupe keys have been *confirmed* on the
calendar. It is written after **every** successful event, not at the end of the
batch, because the failure this is designed for (rate-limit exhaustion, network
drop, SIGKILL) happens mid-batch by definition.

Two properties the format guarantees:

* **Crash safety.** Writes go to a temp file in the same directory and are
  moved into place with ``os.replace``, which is atomic on POSIX and Windows.
  A crash mid-write leaves the previous good state, never a truncated file.
* **Per-calendar isolation.** State is namespaced by calendar id, so syncing
  the same syllabus to a personal and a shared calendar does not make the
  second run think it already finished.

This module deliberately has **no Google dependencies** — it can be imported,
tested, and inspected without any credentials.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Bumped if the on-disk shape ever changes incompatibly. An unknown version
#: is refused rather than misread.
STATE_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SyncedRecord:
    """One confirmed calendar event."""

    event_id: str
    synced_at: str
    course_name: Optional[str] = None
    task_name: Optional[str] = None
    due_date: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "synced_at": self.synced_at,
            "course_name": self.course_name,
            "task_name": self.task_name,
            "due_date": self.due_date,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SyncedRecord":
        return cls(
            event_id=data.get("event_id", ""),
            synced_at=data.get("synced_at", ""),
            course_name=data.get("course_name"),
            task_name=data.get("task_name"),
            due_date=data.get("due_date"),
        )


class StateFileError(RuntimeError):
    """The state file exists but cannot be trusted.

    Raised rather than silently starting from scratch: an unreadable state file
    plus a fresh start equals a calendar full of duplicates.
    """


@dataclass
class SyncState:
    """Load/mutate/persist the checkpoint file.

    Typical use::

        state = SyncState.load("sync_state.json")
        if state.is_synced(key, calendar_id):
            continue          # already done on a previous run
        ...create the event...
        state.mark_synced(key, calendar_id, record)
        state.save()          # after *each* event, not at the end
    """

    path: Path
    calendars: Dict[str, Dict[str, SyncedRecord]] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "SyncState":
        """Load state, or return an empty state when the file does not exist.

        A *missing* file is normal (first run). A *corrupt* file is not, and
        raises — see :class:`StateFileError`.
        """
        state_path = Path(path).expanduser()
        if not state_path.exists():
            logger.info("no checkpoint at %s; starting a fresh sync", state_path)
            return cls(path=state_path)

        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateFileError(
                f"checkpoint file {state_path} is unreadable ({exc}). Refusing to "
                "start fresh, which would duplicate every existing event. Inspect "
                "or delete the file deliberately, then re-run."
            ) from exc

        version = raw.get("version")
        if version != STATE_VERSION:
            raise StateFileError(
                f"checkpoint file {state_path} has version {version!r}, expected "
                f"{STATE_VERSION}. Refusing to guess at its meaning."
            )

        calendars: Dict[str, Dict[str, SyncedRecord]] = {}
        for calendar_id, entries in (raw.get("calendars") or {}).items():
            calendars[calendar_id] = {
                key: SyncedRecord.from_dict(value) for key, value in (entries or {}).items()
            }

        state = cls(
            path=state_path,
            calendars=calendars,
            created_at=raw.get("created_at", _utc_now()),
            updated_at=raw.get("updated_at", _utc_now()),
        )
        logger.info(
            "loaded checkpoint %s: %d calendar(s), %d synced task(s)",
            state_path,
            len(state.calendars),
            sum(len(v) for v in state.calendars.values()),
        )
        return state

    def save(self) -> None:
        """Persist atomically. Safe to call after every single event."""
        self.updated_at = _utc_now()
        payload = {
            "version": STATE_VERSION,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "calendars": {
                calendar_id: {key: record.to_dict() for key, record in entries.items()}
                for calendar_id, entries in self.calendars.items()
            },
        }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Temp file must live in the same directory: os.replace is only atomic
        # within a filesystem.
        handle, temp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())  # survive a power loss, not just a crash
            os.replace(temp_name, self.path)
        except BaseException:
            # Includes KeyboardInterrupt: never leave a stray temp file behind.
            Path(temp_name).unlink(missing_ok=True)
            raise

    # -- queries and mutations --------------------------------------------

    def is_synced(self, dedupe_key: str, calendar_id: str) -> bool:
        return dedupe_key in self.calendars.get(calendar_id, {})

    def get(self, dedupe_key: str, calendar_id: str) -> Optional[SyncedRecord]:
        return self.calendars.get(calendar_id, {}).get(dedupe_key)

    def mark_synced(
        self,
        dedupe_key: str,
        calendar_id: str,
        *,
        event_id: str,
        course_name: Optional[str] = None,
        task_name: Optional[str] = None,
        due_date: Optional[str] = None,
    ) -> SyncedRecord:
        """Record a confirmed event. Call only *after* the API confirms it."""
        record = SyncedRecord(
            event_id=event_id,
            synced_at=_utc_now(),
            course_name=course_name,
            task_name=task_name,
            due_date=due_date,
        )
        self.calendars.setdefault(calendar_id, {})[dedupe_key] = record
        return record

    def forget(self, dedupe_key: str, calendar_id: str) -> None:
        """Drop a key so the next run re-creates it.

        For the case where someone deleted the event in Google Calendar and
        wants it back.
        """
        self.calendars.get(calendar_id, {}).pop(dedupe_key, None)

    def synced_count(self, calendar_id: Optional[str] = None) -> int:
        if calendar_id is None:
            return sum(len(entries) for entries in self.calendars.values())
        return len(self.calendars.get(calendar_id, {}))


class DbSyncState:
    """The same checkpoint, kept in the web app's database.

    A serverless function has no disk that outlives a request, so the
    ``sync_state.json`` file is not an option there. This class has the methods
    ``CalendarSyncer`` uses, and writes **through**: ``mark_synced`` commits
    immediately, so — as with the file — a crash one event later cannot lose
    the record of this one. ``store`` is the web app's ``Store`` (duck-typed so
    this module keeps no database dependency).
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    def is_synced(self, dedupe_key: str, calendar_id: str) -> bool:
        return self.get(dedupe_key, calendar_id) is not None

    def get(self, dedupe_key: str, calendar_id: str) -> Optional[SyncedRecord]:
        for row in self._store.checkpoint_rows(calendar_id):
            if row["dedupe_key"] == dedupe_key:
                return SyncedRecord(
                    event_id=row["event_id"],
                    synced_at=row["synced_at"],
                    course_name=row["course_name"],
                    task_name=row["task_name"],
                    due_date=row["due_date"],
                )
        return None

    def mark_synced(
        self,
        dedupe_key: str,
        calendar_id: str,
        *,
        event_id: str,
        course_name: Optional[str] = None,
        task_name: Optional[str] = None,
        due_date: Optional[str] = None,
    ) -> SyncedRecord:
        record = SyncedRecord(
            event_id=event_id,
            synced_at=_utc_now(),
            course_name=course_name,
            task_name=task_name,
            due_date=due_date,
        )
        self._store.checkpoint_put(
            calendar_id,
            dedupe_key,
            event_id=event_id,
            synced_at=record.synced_at,
            course_name=course_name,
            task_name=task_name,
            due_date=due_date,
        )
        return record

    def forget(self, dedupe_key: str, calendar_id: str) -> None:
        self._store.checkpoint_delete(calendar_id, dedupe_key)

    def save(self) -> None:
        """No-op: every ``mark_synced`` already committed."""
