"""Logging configuration.

Stage boundaries are logged at INFO so a normal run reads as a narrative of
what the pipeline decided and why — which matters when the answer to "why
wasn't my midterm added?" is three stages upstream.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional


def configure_logging(verbose: bool = False, log_file: Optional[Path] = None) -> None:
    """Set up root logging. Idempotent — safe to call more than once."""
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(formatter)
    console.setLevel(level)
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)  # always verbose on disk
        root.addHandler(file_handler)

    # Third-party libraries are chatty and mostly uninteresting; the Google
    # discovery cache warning in particular is pure noise.
    for noisy in ("googleapiclient", "google_auth_httplib2", "urllib3", "httpx", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
