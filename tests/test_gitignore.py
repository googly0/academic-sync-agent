"""Asserts the ignore rules actually ignore, by asking git rather than reading.

This exists because .gitignore has already failed once here in a way review
missed: trailing comments on pattern lines (`token.json   # live credential`)
made git read the whole line as a literal filename, so every credential pattern
was inert while looking correct. Parsing the file ourselves would have repeated
the same misreading — only `git check-ignore` is authoritative.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO_ROOT / ".git").exists(),
    reason="needs a git checkout to ask git what it would ignore",
)


def _is_ignored(relative_path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--", relative_path],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if result.returncode not in (0, 1):
        pytest.fail(f"git check-ignore failed: {result.stderr.decode(errors='replace')}")
    return result.returncode == 0


@pytest.mark.parametrize(
    "path",
    [
        # Live credentials.
        "token.json",
        "credentials.json",
        # What Google Cloud Console actually names the download. The README
        # says to rename it to credentials.json; users routinely do not.
        "client_secret_123-abc.apps.googleusercontent.com.json",
        ".env",
        ".env.local",
        "key.pem",
        # Run artefacts.
        "sync_state.json",
        "output/extracted_tasks.json",
        "output/needs_review.json",
        "run.log",
        # Environment.
        ".venv/pyvenv.cfg",
        "academic_sync/__pycache__/task.cpython-311.pyc",
    ],
)
def test_sensitive_and_generated_paths_are_ignored(path):
    assert _is_ignored(path), f"{path!r} would be committable"


@pytest.mark.parametrize(
    "path",
    ["main.py", "README.md", ".env.example", "academic_sync/models/task.py"],
)
def test_project_files_are_not_ignored(path):
    """The negation guarding .env.example has to survive the .env.* pattern."""
    assert not _is_ignored(path), f"{path!r} is ignored but is part of the project"
