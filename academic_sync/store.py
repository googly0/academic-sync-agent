"""Persistent local store for the web app: sources, tasks, and sync status.

The CLI never touches this. It exists so the web UI is an app you come back to
rather than a form that forgets everything on reload.

Design notes
------------
* **Tasks are stored as validated ``AcademicTask`` JSON.** Every write goes
  through the model, so its validator — the review gate — runs on every
  insert and every human edit. There is no way to store a task whose review
  flags disagree with its data.
* **Imports are idempotent.** Each task carries an ``identity`` fixed at
  insert time: its ``sync_dedupe_key`` when it has one, otherwise a key over
  (course, task, verbatim date phrase). Re-importing the same syllabus or
  rescanning the same email inserts nothing new — and in particular never
  resurrects a task the user dismissed.
* **One connection, one lock.** Import jobs run on worker threads; SQLite is
  happy with that as long as writes are serialised, and the volume here
  (hundreds of rows) makes anything cleverer pointless.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models.task import AcademicTask

#: Task lifecycle. ``active`` passed the review gate; ``review`` is waiting for
#: a human; ``dismissed`` is hidden but kept so a re-import cannot revive it.
STATUS_ACTIVE = "active"
STATUS_REVIEW = "review"
STATUS_DISMISSED = "dismissed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,          -- pdf | image | gmail | manual
    label       TEXT NOT NULL,
    external_id TEXT,                   -- e.g. a Gmail message id
    meta        TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS sources_external
    ON sources(kind, external_id) WHERE external_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY,
    source_id  INTEGER REFERENCES sources(id),
    identity   TEXT NOT NULL UNIQUE,
    status     TEXT NOT NULL,
    task_json  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS syncs (
    task_id    INTEGER NOT NULL REFERENCES tasks(id),
    target     TEXT NOT NULL,           -- gcal | notion
    dedupe_key TEXT,
    remote_id  TEXT,
    remote_url TEXT,
    synced_at  TEXT,
    last_error TEXT,
    PRIMARY KEY (task_id, target)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def task_identity(task: AcademicTask) -> str:
    """The key that makes re-imports idempotent. See the module docstring."""
    if task.sync_dedupe_key:
        return "k:" + task.sync_dedupe_key
    payload = "|".join(
        [_norm(task.course_name), _norm(task.task_name), _norm(task.raw_date_expression)]
    )
    return "r:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def status_for(task: AcademicTask) -> str:
    return STATUS_REVIEW if task.requires_manual_review else STATUS_ACTIVE


@dataclass
class StoredTask:
    id: int
    source_id: Optional[int]
    status: str
    task: AcademicTask
    created_at: str
    updated_at: str


class Store:
    """SQLite-backed workspace state. Safe to share across threads."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
        if new_file:
            # It may hold a Notion token; keep it owner-only like token.json.
            try:
                self.path.chmod(0o600)
            except OSError:  # pragma: no cover - non-POSIX filesystems
                pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- settings ----------------------------------------------------------

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_settings(self, values: Dict[str, Optional[str]]) -> None:
        with self._lock, self._conn:
            for key, value in values.items():
                if value is None:
                    self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
                else:
                    self._conn.execute(
                        "INSERT INTO settings(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, value),
                    )

    # -- sources -----------------------------------------------------------

    def add_source(
        self,
        kind: str,
        label: str,
        *,
        external_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO sources(kind, label, external_id, meta, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (kind, label, external_id, json.dumps(meta or {}), _now()),
            )
            return int(cur.lastrowid)

    def has_source(self, kind: str, external_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM sources WHERE kind = ? AND external_id = ?",
                (kind, external_id),
            ).fetchone()
        return row is not None

    def sources(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.*, COUNT(t.id) AS task_count FROM sources s "
                "LEFT JOIN tasks t ON t.source_id = s.id "
                "GROUP BY s.id ORDER BY s.id DESC"
            ).fetchall()
        return [
            {**dict(r), "meta": json.loads(r["meta"] or "{}")} for r in rows
        ]

    # -- tasks -------------------------------------------------------------

    def add_tasks(
        self, tasks: Iterable[AcademicTask], *, source_id: Optional[int]
    ) -> Dict[str, int]:
        """Insert tasks, skipping any whose identity is already stored.

        Returns counts of ``added`` and ``duplicates`` for the UI to report.
        """
        added = duplicates = 0
        now = _now()
        with self._lock, self._conn:
            for task in tasks:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO tasks"
                    "(source_id, identity, status, task_json, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        source_id,
                        task_identity(task),
                        status_for(task),
                        _dump(task),
                        now,
                        now,
                    ),
                )
                if cur.rowcount:
                    added += 1
                else:
                    duplicates += 1
        return {"added": added, "duplicates": duplicates}

    def get_task(self, task_id: int) -> Optional[StoredTask]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _row_to_task(row) if row else None

    def tasks(self, *, include_dismissed: bool = False) -> List[StoredTask]:
        query = "SELECT * FROM tasks"
        if not include_dismissed:
            query += f" WHERE status != '{STATUS_DISMISSED}'"
        with self._lock:
            rows = self._conn.execute(query + " ORDER BY id").fetchall()
        return [_row_to_task(r) for r in rows]

    def replace_task(self, task_id: int, task: AcademicTask) -> StoredTask:
        """Store a human-corrected task. Its status is recomputed by the gate.

        The identity is deliberately left unchanged, so re-importing the source
        the task originally came from still recognises it.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tasks SET task_json = ?, status = ?, updated_at = ? WHERE id = ?",
                (_dump(task), status_for(task), _now(), task_id),
            )
        stored = self.get_task(task_id)
        assert stored is not None
        return stored

    def dismiss_task(self, task_id: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (STATUS_DISMISSED, _now(), task_id),
            )
        return bool(cur.rowcount)

    # -- sync status -------------------------------------------------------

    def record_sync(
        self,
        task_id: int,
        target: str,
        *,
        dedupe_key: Optional[str],
        remote_id: Optional[str] = None,
        remote_url: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Record a confirmed sync, or an error against the target.

        An error never erases an earlier success: if the task was synced under
        the same key before, that record stands and only ``last_error`` changes.
        """
        with self._lock, self._conn:
            if error is not None:
                self._conn.execute(
                    "INSERT INTO syncs(task_id, target, dedupe_key, last_error) "
                    "VALUES(?, ?, ?, ?) ON CONFLICT(task_id, target) "
                    "DO UPDATE SET last_error = excluded.last_error",
                    (task_id, target, dedupe_key, error),
                )
                return
            self._conn.execute(
                "INSERT INTO syncs(task_id, target, dedupe_key, remote_id, remote_url, "
                "synced_at, last_error) VALUES(?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(task_id, target) DO UPDATE SET "
                "dedupe_key = excluded.dedupe_key, remote_id = excluded.remote_id, "
                "remote_url = excluded.remote_url, synced_at = excluded.synced_at, "
                "last_error = NULL",
                (task_id, target, dedupe_key, remote_id, remote_url, _now()),
            )

    def syncs_for(self, task_ids: Optional[Iterable[int]] = None) -> Dict[int, Dict[str, Dict[str, Any]]]:
        """``{task_id: {target: record}}``."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM syncs").fetchall()
        wanted = set(task_ids) if task_ids is not None else None
        out: Dict[int, Dict[str, Dict[str, Any]]] = {}
        for r in rows:
            if wanted is not None and r["task_id"] not in wanted:
                continue
            out.setdefault(r["task_id"], {})[r["target"]] = dict(r)
        return out


def _dump(task: AcademicTask) -> str:
    # ``sync_dedupe_key`` is computed; the model forbids it as input, so it
    # must not be persisted or the row could never be read back.
    return task.model_dump_json(exclude={"sync_dedupe_key"})


def _row_to_task(row: sqlite3.Row) -> StoredTask:
    return StoredTask(
        id=row["id"],
        source_id=row["source_id"],
        status=row["status"],
        task=AcademicTask.model_validate_json(row["task_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
