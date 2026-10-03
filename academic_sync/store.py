"""Persistent store for the web app: sources, tasks, sync status, jobs, secrets.

The CLI never touches this. It exists so the web UI is an app you come back to
rather than a form that forgets everything on reload.

Design notes
------------
* **One code path, two databases.** Locally this is a SQLite file; hosted on
  Vercel it is Postgres (Neon). Everything goes through SQLAlchemy with plain
  SQL that both dialects accept (``ON CONFLICT``, ``RETURNING`` on Postgres),
  so there is no second implementation to drift out of step.
* **Tasks are stored as validated ``AcademicTask`` JSON.** Every write goes
  through the model, so its validator — the review gate — runs on every
  insert and every human edit. There is no way to store a task whose review
  flags disagree with its data.
* **Imports are idempotent.** Each task carries an ``identity`` fixed at
  insert time: its ``sync_dedupe_key`` when it has one, otherwise a key over
  (course, task, verbatim date phrase). Re-importing the same syllabus or
  rescanning the same email inserts nothing new — and in particular never
  resurrects a task the user dismissed.
* **Jobs and the Calendar checkpoint live here too.** A serverless function
  has no memory between requests, so job progress and "which events have been
  confirmed" must be in the database for a second request to read them.
* **Serialised access.** Import jobs may run on worker threads; one lock keeps
  writes ordered. The volume (hundreds of rows) makes anything cleverer
  pointless.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.pool import NullPool

from .models.task import AcademicTask

#: Task lifecycle. ``active`` passed the review gate; ``review`` is waiting for
#: a human; ``dismissed`` is hidden but kept so a re-import cannot revive it.
STATUS_ACTIVE = "active"
STATUS_REVIEW = "review"
STATUS_DISMISSED = "dismissed"

metadata = sa.MetaData()

settings_t = sa.Table(
    "settings",
    metadata,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("value", sa.Text, nullable=False),
)
sources_t = sa.Table(
    "sources",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("kind", sa.Text, nullable=False),  # pdf | image | gmail | manual
    sa.Column("label", sa.Text, nullable=False),
    sa.Column("external_id", sa.Text),  # e.g. a Gmail message id
    sa.Column("meta", sa.Text, nullable=False, server_default="{}"),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Index(
        "sources_external",
        "kind",
        "external_id",
        unique=True,
        sqlite_where=sa.text("external_id IS NOT NULL"),
        postgresql_where=sa.text("external_id IS NOT NULL"),
    ),
)
tasks_t = sa.Table(
    "tasks",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id")),
    sa.Column("identity", sa.Text, nullable=False, unique=True),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("task_json", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
)
syncs_t = sa.Table(
    "syncs",
    metadata,
    sa.Column("task_id", sa.Integer, sa.ForeignKey("tasks.id"), primary_key=True),
    sa.Column("target", sa.Text, primary_key=True),  # gcal | notion
    sa.Column("dedupe_key", sa.Text),
    sa.Column("remote_id", sa.Text),
    sa.Column("remote_url", sa.Text),
    sa.Column("synced_at", sa.Text),
    sa.Column("last_error", sa.Text),
)
jobs_t = sa.Table(
    "jobs",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("label", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("stages", sa.Text, nullable=False, server_default="{}"),
    sa.Column("result", sa.Text),
    sa.Column("error", sa.Text),
    sa.Column("created_at", sa.Text, nullable=False),
)
secrets_t = sa.Table(
    "secrets",
    metadata,
    sa.Column("name", sa.Text, primary_key=True),
    sa.Column("ciphertext", sa.Text, nullable=False),
)
checkpoint_t = sa.Table(
    "calendar_checkpoint",
    metadata,
    sa.Column("calendar_id", sa.Text, primary_key=True),
    sa.Column("dedupe_key", sa.Text, primary_key=True),
    sa.Column("event_id", sa.Text, nullable=False),
    sa.Column("synced_at", sa.Text, nullable=False),
    sa.Column("course_name", sa.Text),
    sa.Column("task_name", sa.Text),
    sa.Column("due_date", sa.Text),
)


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


def database_url(value: str) -> str:
    """Normalise a connection string for SQLAlchemy + psycopg 3.

    Neon and Vercel hand out ``postgres://`` or ``postgresql://``; SQLAlchemy
    would pick the older psycopg2 driver for those.
    """
    value = value.strip()
    for prefix in ("postgres://", "postgresql://"):
        if value.startswith(prefix):
            return "postgresql+psycopg://" + value[len(prefix):]
    return value


class Store:
    """Workspace state over SQLite (a path) or Postgres (a URL)."""

    def __init__(self, path: str | Path) -> None:
        """Open a local SQLite database file, creating it if needed."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists()
        engine = sa.create_engine(
            f"sqlite:///{path}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_conn: Any, _record: Any) -> None:
            dbapi_conn.execute("PRAGMA foreign_keys = ON")

        self._init(engine)
        if new_file:
            # It may hold a Notion token; keep it owner-only like token.json.
            try:
                path.chmod(0o600)
            except OSError:  # pragma: no cover - non-POSIX filesystems
                pass
        self.path: Optional[Path] = path

    @classmethod
    def from_url(cls, url: str) -> "Store":
        """Open a database by connection URL (Postgres in production)."""
        store = cls.__new__(cls)
        url = database_url(url)
        if url.startswith("sqlite"):
            engine = sa.create_engine(url, connect_args={"check_same_thread": False})
        else:
            # No pooling: serverless instances are short-lived, and Neon's own
            # pooler sits in front. A held connection would just go stale.
            engine = sa.create_engine(url, poolclass=NullPool)
        store._init(engine)
        store.path = None
        return store

    def _init(self, engine: sa.Engine) -> None:
        self._engine = engine
        self._lock = threading.Lock()
        self._postgres = engine.dialect.name == "postgresql"
        metadata.create_all(engine)

    def close(self) -> None:
        with self._lock:
            self._engine.dispose()

    # -- plumbing ------------------------------------------------------------

    def _read(self, sql: str, **params: Any) -> List[Dict[str, Any]]:
        with self._lock, self._engine.connect() as conn:
            return [dict(r) for r in conn.execute(sa.text(sql), params).mappings()]

    def _write(self, sql: str, **params: Any) -> int:
        """Run one write in its own transaction; returns the affected row count."""
        with self._lock, self._engine.begin() as conn:
            return conn.execute(sa.text(sql), params).rowcount

    def _insert_returning_id(self, conn: sa.Connection, sql: str, **params: Any) -> int:
        if self._postgres:
            return int(conn.execute(sa.text(sql + " RETURNING id"), params).scalar_one())
        return int(conn.execute(sa.text(sql), params).lastrowid)

    # -- settings ------------------------------------------------------------

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        rows = self._read("SELECT value FROM settings WHERE key = :key", key=key)
        return rows[0]["value"] if rows else default

    def set_settings(self, values: Dict[str, Optional[str]]) -> None:
        with self._lock, self._engine.begin() as conn:
            for key, value in values.items():
                if value is None:
                    conn.execute(sa.text("DELETE FROM settings WHERE key = :key"), {"key": key})
                else:
                    conn.execute(
                        sa.text(
                            "INSERT INTO settings(key, value) VALUES(:key, :value) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                        ),
                        {"key": key, "value": value},
                    )

    # -- secrets (already encrypted by the caller) -----------------------------

    def put_secret(self, name: str, ciphertext: str) -> None:
        self._write(
            "INSERT INTO secrets(name, ciphertext) VALUES(:name, :c) "
            "ON CONFLICT(name) DO UPDATE SET ciphertext = excluded.ciphertext",
            name=name,
            c=ciphertext,
        )

    def get_secret(self, name: str) -> Optional[str]:
        rows = self._read("SELECT ciphertext FROM secrets WHERE name = :name", name=name)
        return rows[0]["ciphertext"] if rows else None

    def delete_secret(self, name: str) -> None:
        self._write("DELETE FROM secrets WHERE name = :name", name=name)

    # -- sources -------------------------------------------------------------

    def add_source(
        self,
        kind: str,
        label: str,
        *,
        external_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> int:
        with self._lock, self._engine.begin() as conn:
            return self._insert_returning_id(
                conn,
                "INSERT INTO sources(kind, label, external_id, meta, created_at) "
                "VALUES(:kind, :label, :ext, :meta, :now)",
                kind=kind,
                label=label,
                ext=external_id,
                meta=json.dumps(meta or {}),
                now=_now(),
            )

    def has_source(self, kind: str, external_id: str) -> bool:
        return bool(
            self._read(
                "SELECT 1 AS one FROM sources WHERE kind = :kind AND external_id = :ext",
                kind=kind,
                ext=external_id,
            )
        )

    def sources(self) -> List[Dict[str, Any]]:
        rows = self._read(
            "SELECT s.id, s.kind, s.label, s.external_id, s.meta, s.created_at, "
            "COUNT(t.id) AS task_count FROM sources s "
            "LEFT JOIN tasks t ON t.source_id = s.id "
            "GROUP BY s.id, s.kind, s.label, s.external_id, s.meta, s.created_at "
            "ORDER BY s.id DESC"
        )
        return [{**r, "meta": json.loads(r["meta"] or "{}")} for r in rows]

    # -- tasks ---------------------------------------------------------------

    def add_tasks(
        self, tasks: Iterable[AcademicTask], *, source_id: Optional[int]
    ) -> Dict[str, int]:
        """Insert tasks, skipping any whose identity is already stored.

        Returns counts of ``added`` and ``duplicates`` for the UI to report.
        """
        added = duplicates = 0
        now = _now()
        with self._lock, self._engine.begin() as conn:
            for task in tasks:
                result = conn.execute(
                    sa.text(
                        "INSERT INTO tasks"
                        "(source_id, identity, status, task_json, created_at, updated_at) "
                        "VALUES(:src, :ident, :status, :json, :now, :now) "
                        "ON CONFLICT(identity) DO NOTHING"
                    ),
                    {
                        "src": source_id,
                        "ident": task_identity(task),
                        "status": status_for(task),
                        "json": _dump(task),
                        "now": now,
                    },
                )
                if result.rowcount:
                    added += 1
                else:
                    duplicates += 1
        return {"added": added, "duplicates": duplicates}

    def get_task(self, task_id: int) -> Optional[StoredTask]:
        rows = self._read("SELECT * FROM tasks WHERE id = :id", id=task_id)
        return _row_to_task(rows[0]) if rows else None

    def tasks(self, *, include_dismissed: bool = False) -> List[StoredTask]:
        if include_dismissed:
            rows = self._read("SELECT * FROM tasks ORDER BY id")
        else:
            rows = self._read(
                "SELECT * FROM tasks WHERE status != :dismissed ORDER BY id",
                dismissed=STATUS_DISMISSED,
            )
        return [_row_to_task(r) for r in rows]

    def replace_task(self, task_id: int, task: AcademicTask) -> StoredTask:
        """Store a human-corrected task. Its status is recomputed by the gate.

        The identity is deliberately left unchanged, so re-importing the source
        the task originally came from still recognises it.
        """
        self._write(
            "UPDATE tasks SET task_json = :json, status = :status, updated_at = :now "
            "WHERE id = :id",
            json=_dump(task),
            status=status_for(task),
            now=_now(),
            id=task_id,
        )
        stored = self.get_task(task_id)
        assert stored is not None
        return stored

    def dismiss_task(self, task_id: int) -> bool:
        return bool(
            self._write(
                "UPDATE tasks SET status = :status, updated_at = :now WHERE id = :id",
                status=STATUS_DISMISSED,
                now=_now(),
                id=task_id,
            )
        )

    # -- sync status ---------------------------------------------------------

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
        if error is not None:
            self._write(
                "INSERT INTO syncs(task_id, target, dedupe_key, last_error) "
                "VALUES(:t, :target, :key, :err) ON CONFLICT(task_id, target) "
                "DO UPDATE SET last_error = excluded.last_error",
                t=task_id,
                target=target,
                key=dedupe_key,
                err=error,
            )
            return
        self._write(
            "INSERT INTO syncs(task_id, target, dedupe_key, remote_id, remote_url, "
            "synced_at, last_error) VALUES(:t, :target, :key, :rid, :url, :now, NULL) "
            "ON CONFLICT(task_id, target) DO UPDATE SET "
            "dedupe_key = excluded.dedupe_key, remote_id = excluded.remote_id, "
            "remote_url = excluded.remote_url, synced_at = excluded.synced_at, "
            "last_error = NULL",
            t=task_id,
            target=target,
            key=dedupe_key,
            rid=remote_id,
            url=remote_url,
            now=_now(),
        )

    def syncs_for(
        self, task_ids: Optional[Iterable[int]] = None
    ) -> Dict[int, Dict[str, Dict[str, Any]]]:
        """``{task_id: {target: record}}``."""
        rows = self._read("SELECT * FROM syncs")
        wanted = set(task_ids) if task_ids is not None else None
        out: Dict[int, Dict[str, Dict[str, Any]]] = {}
        for r in rows:
            if wanted is not None and r["task_id"] not in wanted:
                continue
            out.setdefault(r["task_id"], {})[r["target"]] = r
        return out

    # -- jobs ----------------------------------------------------------------

    def create_job(self, job_id: str, kind: str, label: str) -> None:
        self._write(
            "INSERT INTO jobs(id, kind, label, status, stages, created_at) "
            "VALUES(:id, :kind, :label, 'queued', '{}', :now)",
            id=job_id,
            kind=kind,
            label=label,
            now=_now(),
        )
        # Finished jobs are only kept so the browser can poll for the result.
        self._write(
            "DELETE FROM jobs WHERE id NOT IN "
            "(SELECT id FROM jobs ORDER BY created_at DESC, id DESC LIMIT 40)"
        )

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        rows = self._read("SELECT * FROM jobs WHERE id = :id", id=job_id)
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r["id"],
            "kind": r["kind"],
            "label": r["label"],
            "status": r["status"],
            "stages": json.loads(r["stages"] or "{}"),
            "result": json.loads(r["result"]) if r["result"] else None,
            "error": r["error"],
        }

    def update_job(
        self,
        job_id: str,
        *,
        status: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        sets, params = [], {"id": job_id}
        if status is not None:
            sets.append("status = :status")
            params["status"] = status
        if result is not None:
            sets.append("result = :result")
            params["result"] = json.dumps(result)
        if error is not None:
            sets.append("error = :error")
            params["error"] = error
        if sets:
            self._write(f"UPDATE jobs SET {', '.join(sets)} WHERE id = :id", **params)

    def set_job_stage(self, job_id: str, stage: str, state: str, message: str) -> None:
        """Read-modify-write one stage. Only one worker ever writes a given
        job, so there is no concurrent update of the same row to guard."""
        job = self.get_job(job_id)
        if job is None:
            return
        stages = dict(job["stages"])
        stages[stage] = {"state": state, "message": message}
        self._write(
            "UPDATE jobs SET stages = :stages WHERE id = :id",
            stages=json.dumps(stages),
            id=job_id,
        )

    # -- calendar checkpoint ---------------------------------------------------

    def checkpoint_rows(self, calendar_id: str) -> List[Dict[str, Any]]:
        return self._read(
            "SELECT * FROM calendar_checkpoint WHERE calendar_id = :c", c=calendar_id
        )

    def checkpoint_put(
        self,
        calendar_id: str,
        dedupe_key: str,
        *,
        event_id: str,
        synced_at: str,
        course_name: Optional[str],
        task_name: Optional[str],
        due_date: Optional[str],
    ) -> None:
        self._write(
            "INSERT INTO calendar_checkpoint(calendar_id, dedupe_key, event_id, synced_at, "
            "course_name, task_name, due_date) VALUES(:cal, :key, :ev, :at, :course, :task, :due) "
            "ON CONFLICT(calendar_id, dedupe_key) DO UPDATE SET event_id = excluded.event_id, "
            "synced_at = excluded.synced_at, course_name = excluded.course_name, "
            "task_name = excluded.task_name, due_date = excluded.due_date",
            cal=calendar_id,
            key=dedupe_key,
            ev=event_id,
            at=synced_at,
            course=course_name,
            task=task_name,
            due=due_date,
        )

    def checkpoint_delete(self, calendar_id: str, dedupe_key: str) -> None:
        self._write(
            "DELETE FROM calendar_checkpoint WHERE calendar_id = :c AND dedupe_key = :k",
            c=calendar_id,
            k=dedupe_key,
        )


def _dump(task: AcademicTask) -> str:
    # ``sync_dedupe_key`` is computed; the model forbids it as input, so it
    # must not be persisted or the row could never be read back.
    return task.model_dump_json(exclude={"sync_dedupe_key"})


def _row_to_task(row: Dict[str, Any]) -> StoredTask:
    return StoredTask(
        id=row["id"],
        source_id=row["source_id"],
        status=row["status"],
        task=AcademicTask.model_validate_json(row["task_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
