"""Vercel installs from pyproject.toml; local installs use requirements.txt.
Two lists of dependencies will drift unless something checks them."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent


def _requirements() -> set[str]:
    out = set()
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.add(line)
    return out


@pytest.fixture(scope="module")
def pyproject():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_pyproject_and_requirements_list_the_same_dependencies(pyproject):
    assert set(pyproject["project"]["dependencies"]) == _requirements()


def test_vercel_is_told_which_app_to_serve(pyproject):
    assert pyproject["tool"]["vercel"]["entrypoint"] == "app:app"
    assert (ROOT / "app.py").is_file()


def test_runtime_requirements_contain_nothing_native(pyproject):
    """Vercel cannot run Tesseract or Poppler, nor should the bundle carry
    test tooling."""
    names = {re.split(r"[<>=\[ ]", d, 1)[0].lower() for d in pyproject["project"]["dependencies"]}
    assert not names & {"pytesseract", "pdf2image", "pytest", "pgserver"}
