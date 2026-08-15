"""Stage 5 — Google Calendar sync.

``state`` is dependency-free and always importable. ``auth`` and
``google_calendar`` pull in the Google client libraries, so they are imported
lazily by the orchestrator and never touched on a ``--dry-run``.
"""

from .errors import (
    CalendarAuthError,
    CalendarSyncError,
    NonRetryableAPIError,
    RateLimitExhaustedError,
)
from .state import StateFileError, SyncedRecord, SyncState

__all__ = [
    "SyncState",
    "SyncedRecord",
    "StateFileError",
    "CalendarSyncError",
    "CalendarAuthError",
    "RateLimitExhaustedError",
    "NonRetryableAPIError",
]
