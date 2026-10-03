"""Tests for the app's service layer: imports, human fixes, and sync.

All offline: the stub LLM backend, a fake Calendar service, a fake Notion
session, and a fake Gmail source.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from academic_sync.extraction.pdf_extractor import PageText
from academic_sync.orchestrator import analyze_pages
from academic_sync.sources.gmail import EmailDoc
from academic_sync.store import STATUS_ACTIVE, STATUS_REVIEW, Store
from academic_sync.workspace import Workspace, WorkspaceError, WorkspacePaths

SAMPLE_PDF = Path(__file__).resolve().parent.parent / "examples" / "sample_syllabus_cs231.pdf"


@pytest.fixture
def ws(tmp_path):
    paths = WorkspacePaths(
        db=tmp_path / "app.db",
        credentials=tmp_path / "credentials.json",
        token=tmp_path / "token.json",
        state=tmp_path / "sync_state.json",
    )
    workspace = Workspace(Store(paths.db), paths)
    workspace.update_settings({"semester_start": "2026-01-12", "llm_backend": "stub"})
    yield workspace
    workspace.store.close()


def by_name(ws):
    return {t.task.task_name: t for t in ws.store.tasks()}


class TestAnalyzePages:
    def test_runs_stages_2_to_4_on_plain_text(self, ws):
        config = ws.pipeline_config()
        pages = [PageText(page_number=1, text="PS1 due Week 3 Friday", source="text_layer")]
        result = analyze_pages(config, pages, source_name="email: PS1")
        assert len(result.all_tasks) == 5  # the stub's fixture
        assert {t.task_name for t in result.syncable} >= {"Problem Set 1", "Midterm Exam"}
        assert all(t.requires_manual_review for t in result.needs_review)


class TestImports:
    def test_semester_is_required_before_any_import(self, tmp_path):
        ws = Workspace(Store(tmp_path / "x.db"), WorkspacePaths(db=tmp_path / "x.db"))
        with pytest.raises(WorkspaceError, match="semester start"):
            ws.add_manual({"course_name": "A", "task_name": "B", "date_phrase": "Oct 10"})

    @pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="sample PDF not bundled")
    def test_pdf_import_is_idempotent(self, ws):
        first = ws.import_pdf(SAMPLE_PDF, label="cs231.pdf")
        second = ws.import_pdf(SAMPLE_PDF, label="cs231.pdf")
        assert first["added"] == 5 and first["flagged"] == 2
        assert second["added"] == 0 and second["duplicates"] == 5

    def test_gmail_import_skips_already_scanned_messages(self, ws):
        gmail = FakeGmail(
            {
                "m1": EmailDoc("m1", "PS1 posted", "prof@uni.edu", "Mon, 2 Feb 2026", "due Week 3 Friday"),
                "m2": EmailDoc("m2", "Midterm", "prof@uni.edu", "Tue, 3 Feb 2026", "second Tuesday of October"),
            }
        )
        first = ws.import_gmail(gmail=gmail)
        assert first["new_emails"] == 2
        assert gmail.fetched == ["m1", "m2"]
        second = ws.import_gmail(gmail=gmail)
        assert second["new_emails"] == 0
        assert gmail.fetched == ["m1", "m2"]  # nothing fetched twice

    def test_gmail_task_is_attributed_to_its_email(self, ws):
        gmail = FakeGmail({"m1": EmailDoc("m1", "PS1 posted", "prof@uni.edu", "d", "body")})
        ws.import_gmail(gmail=gmail)
        source = ws.task_views()[0]["source"]
        assert source["kind"] == "gmail" and source["label"] == "PS1 posted"
        assert source["meta"]["from"] == "prof@uni.edu"


class TestManualAndFix:
    def test_manual_task_still_goes_through_the_resolver(self, ws):
        ok = ws.add_manual({"course_name": "MATH 2", "task_name": "Quiz 1", "date_phrase": "Week 2 Wednesday"})
        assert ok.status == STATUS_ACTIVE
        assert ok.task.exact_due_date == date(2026, 1, 21)
        vague = ws.add_manual({"course_name": "MATH 2", "task_name": "Quiz 2", "date_phrase": "Friday"})
        assert vague.status == STATUS_REVIEW

    def test_fixing_the_phrase_resolves_the_task(self, ws):
        task = ws.add_manual({"course_name": "MATH 2", "task_name": "Quiz 2", "date_phrase": "TBD"})
        fixed = ws.fix_task(task.id, {"date_phrase": "Week 4 Friday"})
        assert fixed.status == STATUS_ACTIVE
        assert fixed.task.exact_due_date == date(2026, 2, 6)

    def test_a_bad_phrase_stays_in_review(self, ws):
        task = ws.add_manual({"course_name": "MATH 2", "task_name": "Quiz 2", "date_phrase": "TBD"})
        assert ws.fix_task(task.id, {"date_phrase": "Week 5"}).status == STATUS_REVIEW

    def test_new_wording_does_not_clear_a_contradiction(self, ws):
        ws.import_gmail(gmail=FakeGmail({"m": EmailDoc("m", "s", "f", "d", "b")}))
        lab = by_name(ws)["Lab Report 2"]
        assert lab.status == STATUS_REVIEW
        still = ws.fix_task(lab.id, {"date_phrase": "Oct 17"})
        assert still.status == STATUS_REVIEW
        assert "contradiction" in still.task.review_reason

    def test_picking_a_date_resolves_a_contradiction(self, ws):
        ws.import_gmail(gmail=FakeGmail({"m": EmailDoc("m", "s", "f", "d", "b")}))
        lab = by_name(ws)["Lab Report 2"]
        fixed = ws.fix_task(lab.id, {"date": "2026-10-17"})
        assert fixed.status == STATUS_ACTIVE
        # The source's own wording is kept for provenance.
        assert fixed.task.raw_date_expression == "Oct 10"

    def test_an_edit_cannot_bypass_the_gate(self, ws):
        task = ws.add_manual({"course_name": "MATH 2", "task_name": "Quiz 2", "date_phrase": "Week 4 Friday"})
        assert ws.fix_task(task.id, {"task_name": ""}).status == STATUS_REVIEW

    def test_end_before_start_is_rejected(self, ws):
        task = ws.add_manual({"course_name": "M", "task_name": "Exams", "date_phrase": "TBD"})
        with pytest.raises(WorkspaceError):
            ws.fix_task(task.id, {"date": "2026-05-08", "end_date": "2026-05-04"})

    def test_week_number_is_reported_for_the_timeline(self, ws):
        ws.add_manual({"course_name": "M", "task_name": "Q", "date_phrase": "Week 3 Friday"})
        assert ws.task_views()[0]["week"] == 3


class TestSync:
    def _seed(self, ws):
        ws.add_manual({"course_name": "CS 1", "task_name": "PS1", "date_phrase": "Week 3 Friday"})
        ws.add_manual({"course_name": "CS 1", "task_name": "PS2", "date_phrase": "Week 5 Friday"})
        ws.add_manual({"course_name": "CS 1", "task_name": "Final", "date_phrase": "TBD"})

    def test_calendar_sync_sends_only_active_tasks_and_is_idempotent(self, ws):
        from tests.test_calendar_sync import FakeCalendarService

        self._seed(ws)
        service = FakeCalendarService()
        first = ws.sync(["gcal"], calendar_service=service)["gcal"]
        assert first["created"] == 2
        second = ws.sync(["gcal"], calendar_service=service)["gcal"]
        assert second["created"] == 0 and second["already_synced"] == 2
        views = {v["task_name"]: v for v in ws.task_views()}
        assert views["PS1"]["sync"]["gcal"]["state"] == "synced"
        assert views["Final"]["sync"]["gcal"]["state"] == "pending"

    def test_changing_calendar_shows_tasks_as_unsynced_there(self, ws):
        from tests.test_calendar_sync import FakeCalendarService

        self._seed(ws)
        ws.sync(["gcal"], calendar_service=FakeCalendarService())
        ws.update_settings({"calendar_id": "school@group.calendar.google.com"})
        states = {v["sync"]["gcal"]["state"] for v in ws.task_views() if v["status"] == "active"}
        assert states == {"changed"}

    def test_editing_a_synced_task_marks_it_changed(self, ws):
        from tests.test_calendar_sync import FakeCalendarService

        self._seed(ws)
        ws.sync(["gcal"], calendar_service=FakeCalendarService())
        ps1 = by_name(ws)["PS1"]
        ws.fix_task(ps1.id, {"date_phrase": "Week 4 Friday"})
        view = next(v for v in ws.task_views() if v["id"] == ps1.id)
        assert view["sync"]["gcal"]["state"] == "changed"

    def test_notion_sync_creates_then_skips(self, ws):
        from tests.test_notion_sync import FakeNotion

        self._seed(ws)
        fake = FakeNotion()
        ws.store.set_settings({"notion_token": "secret_x", "notion_database_id": FakeNotion.DB})
        first = ws.sync(["notion"], notion_client=fake.client())["notion"]
        assert first["created"] == 2
        second = ws.sync(["notion"], notion_client=fake.client())["notion"]
        assert second["created"] == 0 and second["already_synced"] == 2
        assert len(fake.pages) == 2

    def test_unknown_target_is_rejected(self, ws):
        with pytest.raises(WorkspaceError):
            ws.sync(["outlook"])

    def test_calendar_without_credentials_is_a_clear_error(self, ws):
        self._seed(ws)
        with pytest.raises(WorkspaceError, match="Connect your Google account"):
            ws.sync(["gcal"])


class FakeGmail:
    def __init__(self, emails):
        self.emails = emails
        self.fetched = []

    def list_ids(self, query, *, max_results=25):
        assert query  # the default query is applied when none is given
        return list(self.emails)[:max_results]

    def fetch(self, message_id):
        self.fetched.append(message_id)
        return self.emails[message_id]
