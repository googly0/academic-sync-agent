"""Vercel entry point. Vercel serves the ASGI ``app`` defined here."""

from academic_sync.web.hosted_app import load

app = load()
