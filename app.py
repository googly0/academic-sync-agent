"""Vercel entry point: Vercel serves the ASGI ``app`` defined here.

Everything real lives in ``academic_sync.web.hosted_app``. Locally, run
``python -m academic_sync.web`` instead — that one needs no database server,
no login, and no environment variables.
"""

from academic_sync.web.hosted_app import load

app = load()
