"""Stage-5 failure types."""

from __future__ import annotations


class CalendarSyncError(RuntimeError):
    """Base class for anything that goes wrong talking to Google Calendar."""


class CalendarAuthError(CalendarSyncError):
    """Credentials are missing, malformed, expired beyond refresh, or the
    OAuth consent flow could not complete."""


class RateLimitExhaustedError(CalendarSyncError):
    """Exponential backoff ran out of attempts on a 429/403-quota error.

    This is the headline resumable failure: the run stops cleanly here, the
    checkpoint is persisted, and a later re-run continues from the next
    unsynced task rather than replaying the whole batch.
    """


class NonRetryableAPIError(CalendarSyncError):
    """A 4xx that will fail identically on retry — bad calendar id, revoked
    scope, malformed event. Retrying wastes quota; we stop and report."""
