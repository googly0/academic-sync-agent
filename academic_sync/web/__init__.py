"""Local web UI for the pipeline.

Optional: the CLI never imports this package, so FastAPI is not required to
run the tool from a terminal. Start it with::

    python -m academic_sync.web

Everything runs locally and every analysis is a dry run — the web UI cannot
write to a calendar, by construction (see ``app.py``).
"""

__all__ = ["create_app"]


def __getattr__(name: str):
    # Lazy so that `import academic_sync.web` does not pull in FastAPI until
    # someone actually asks for the app.
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(name)
