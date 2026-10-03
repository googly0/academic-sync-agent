"""End-to-end tests of the web API through FastAPI's TestClient. Offline."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from academic_sync.web.app import create_app  # noqa: E402
from academic_sync.workspace import WorkspacePaths  # noqa: E402

SAMPLE_PDF = Path(__file__).resolve().parent.parent / "examples" / "sample_syllabus_cs231.pdf"


@pytest.fixture
def app(tmp_path):
    return create_app(
        WorkspacePaths(
            db=tmp_path / "app.db",
            credentials=tmp_path / "credentials.json",
            token=tmp_path / "token.json",
            state=tmp_path / "sync_state.json",
        )
    )


@pytest.fixture
def client(app):
    c = TestClient(app)
    c.headers["X-App-Token"] = app.state.token
    return c


def wait(client, job_id, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def setup_semester(client):
    r = client.post("/api/settings", json={"semester_start": "2026-01-12", "llm_backend": "stub"})
    assert r.status_code == 200, r.text


class TestToken:
    def test_page_embeds_the_token(self, app):
        html = TestClient(app).get("/").text
        assert app.state.token in html and "__APP_TOKEN__" not in html

    def test_mutation_without_token_is_refused(self, app):
        r = TestClient(app).post("/api/settings", json={"semester_start": "2026-01-12"})
        assert r.status_code == 403

    def test_reads_need_no_token(self, app):
        assert TestClient(app).get("/api/state").status_code == 200


def test_first_run_state_has_no_semester(client):
    state = client.get("/api/state").json()
    assert state["semester"] is None and state["tasks"] == []
    assert state["connections"]["google"]["connected"] is False


def test_bad_settings_are_a_400(client):
    assert client.post("/api/settings", json={"semester_start": "next week"}).status_code == 400
    assert client.post("/api/settings", json={"evil": "x"}).status_code == 400


@pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="sample PDF not bundled")
def test_pdf_import_then_fix_then_dismiss(client):
    setup_semester(client)
    with SAMPLE_PDF.open("rb") as fh:
        r = client.post("/api/import/pdf", files={"file": ("cs231.pdf", fh, "application/pdf")})
    job = wait(client, r.json()["job_id"])
    assert job["status"] == "done", job
    assert job["result"]["added"] == 5
    assert job["stages"]["validate"]["state"] == "done"

    tasks = client.get("/api/state").json()["tasks"]
    inbox = [t for t in tasks if t["status"] == "review"]
    final = next(t for t in inbox if t["task_name"] == "Final Project")
    assert "unresolvable_date" in final["review_codes"]

    state = client.patch(f"/api/tasks/{final['id']}", json={"date_phrase": "Week 16 Friday"}).json()
    fixed = next(t for t in state["tasks"] if t["id"] == final["id"])
    assert fixed["status"] == "active" and fixed["exact_due_date"] == "2026-05-01"

    lab = next(t for t in inbox if t["task_name"] == "Lab Report 2")
    state = client.delete(f"/api/tasks/{lab['id']}").json()
    assert all(t["id"] != lab["id"] for t in state["tasks"])


def test_import_without_semester_reports_the_problem(client):
    r = client.post("/api/import/pdf", files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")})
    job = wait(client, r.json()["job_id"])
    assert job["status"] == "error" and "semester start" in job["error"]


def test_non_pdf_and_non_image_uploads_are_rejected(client):
    setup_semester(client)
    assert client.post("/api/import/pdf", files={"file": ("a.txt", b"x")}).status_code == 400
    assert client.post("/api/import/images", files={"files": ("a.txt", b"x")}).status_code == 400


def test_manual_add(client):
    setup_semester(client)
    state = client.post(
        "/api/tasks", json={"course_name": "CS 1", "task_name": "PS1", "date_phrase": "Week 3 Friday"}
    ).json()
    assert state["tasks"][0]["exact_due_date"] == "2026-01-30"
    assert state["tasks"][0]["week"] == 3


def test_sync_without_google_fails_with_guidance(client):
    setup_semester(client)
    client.post("/api/tasks", json={"course_name": "CS 1", "task_name": "PS1", "date_phrase": "Week 3 Friday"})
    job = wait(client, client.post("/api/sync", json={"targets": ["gcal"]}).json()["job_id"])
    assert job["status"] == "error" and "Connect your Google account" in job["error"]


def test_connect_google_without_client_secrets_is_a_400(client):
    r = client.post("/api/connections/google")
    assert r.status_code == 400 and "credentials.json" in r.json()["detail"]


def test_notion_connect_with_a_bad_id_is_a_400(client):
    r = client.post("/api/connections/notion", json={"token": "secret_x", "database_id": "nope"})
    assert r.status_code == 400
