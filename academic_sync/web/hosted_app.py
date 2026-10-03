"""The deployed app: Postgres, Google sign-in, encrypted tokens.

``build_hosted_app`` wires the pieces ``create_app`` needs for a serverless
host. ``app`` is what Vercel serves; see ``api/index.py``.

A misconfigured deploy (a missing variable) must not look like a crash. Rather
than raising at import — which Vercel reports as an opaque 500 — it serves a
503 page naming exactly what to set.
"""

from __future__ import annotations

import logging
import os
from html import escape
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from ..calendar_sync.auth import DbTokenStore
from ..secrets_box import SecretsBox, SecretsError
from ..store import Store
from ..workspace import Workspace
from .app import create_app
from .auth import HostedAuth, HostedConfigError, HostedSettings

logger = logging.getLogger(__name__)


def build_hosted_app(env: Optional[Dict[str, str]] = None) -> Any:
    env = dict(os.environ) if env is None else env
    settings = HostedSettings.from_env(env)

    # Vercel's Neon integration sets DATABASE_URL; POSTGRES_URL is the older name.
    url = env.get("DATABASE_URL") or env.get("POSTGRES_URL")
    if not url:
        raise HostedConfigError(
            "missing environment variable(s): DATABASE_URL (add a Neon database "
            "to the project from the Vercel Marketplace)"
        )
    try:
        box = SecretsBox(env.get("TOKEN_ENCRYPTION_KEY"))
    except SecretsError as exc:
        raise HostedConfigError(str(exc)) from exc

    store = Store.from_url(url)
    token_store = DbTokenStore(store, box)
    workspace = Workspace(store, hosted=True, token_store=token_store, box=box)
    return create_app(workspace=workspace, hosted=HostedAuth(settings, token_store))


def misconfigured_app(problem: str) -> FastAPI:
    """Stand-in served when configuration is incomplete."""
    app = FastAPI(docs_url=None, openapi_url=None)
    page = (
        "<!doctype html><meta charset=utf-8><title>Setup needed</title>"
        "<body style='font:16px/1.5 system-ui;max-width:560px;margin:15vh auto;padding:0 20px'>"
        "<h1>Almost there</h1><p>The app is deployed but not fully configured:</p>"
        f"<pre style='white-space:pre-wrap;background:#eee;padding:12px;border-radius:8px'>{escape(problem)}</pre>"
        "<p>Set it in the Vercel project settings, then redeploy.</p></body>"
    )

    @app.get("/{path:path}")
    def _any(path: str = "") -> Any:  # noqa: ARG001
        return HTMLResponse(page, status_code=503)

    return app


def load() -> Any:
    try:
        return build_hosted_app()
    except HostedConfigError as exc:
        logger.error("hosted app not configured: %s", exc)
        return misconfigured_app(str(exc))
