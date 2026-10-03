"""Reading images with Claude, and the hosted app's PDF/image behaviour."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

import anthropic
from academic_sync.extraction.image_extractor import (
    NO_TEXT,
    TRANSCRIBE_PROMPT,
    VisionImageExtractor,
    _encode_for_api,
)
from academic_sync.extraction.pdf_extractor import PageText, PDFExtractionError
from academic_sync.orchestrator import analyze_pages
from academic_sync.store import Store
from academic_sync.workspace import Workspace, WorkspaceError, WorkspacePaths


class FakeAnthropic:
    """Records requests; replies with queued text or raises."""

    def __init__(self, *replies, stop_reason="end_turn"):
        self.replies = list(replies)
        self.stop_reason = stop_reason
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=reply)], stop_reason=self.stop_reason
        )


@pytest.fixture
def png(tmp_path):
    path = tmp_path / "shot.png"
    Image.new("RGB", (300, 120), "white").save(path)
    return path


class TestVisionReader:
    def test_sends_the_image_and_returns_one_page_per_image(self, png, tmp_path):
        second = tmp_path / "two.png"
        Image.new("RGB", (50, 50), "black").save(second)
        client = FakeAnthropic("PS3 due Week 5 Friday", "Quiz on Oct 12")
        doc = VisionImageExtractor(client=client).extract([png, second])
        assert [p.text for p in doc.pages] == ["PS3 due Week 5 Friday", "Quiz on Oct 12"]
        assert all(p.source == "ocr" for p in doc.pages)
        block = client.requests[0]["messages"][0]["content"][0]
        assert block["type"] == "image" and block["source"]["media_type"] == "image/png"
        assert base64.b64decode(block["source"]["data"])[:4] == b"\x89PNG"

    def test_prompt_forbids_interpreting_dates(self):
        """The same verbatim rule that protects the resolver applies here."""
        assert "character for character" in TRANSCRIBE_PROMPT
        assert "do not correct, reformat, translate or interpret" in TRANSCRIBE_PROMPT

    def test_a_blank_image_is_marked_empty_not_invented(self, png):
        doc = VisionImageExtractor(client=FakeAnthropic(NO_TEXT)).extract([png])
        assert doc.pages[0].source == "empty" and doc.pages[0].text == ""

    def test_truncated_reply_is_an_error(self, png):
        client = FakeAnthropic("partial", stop_reason="max_tokens")
        with pytest.raises(PDFExtractionError, match="cut off"):
            VisionImageExtractor(client=client).extract([png])

    def test_api_failure_is_a_clear_error(self, png):
        err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
        with pytest.raises(PDFExtractionError, match="could not read shot.png"):
            VisionImageExtractor(client=FakeAnthropic(err)).extract([png])

    def test_rejected_key_says_so(self, png):
        response = httpx.Response(401, request=httpx.Request("POST", "https://x"))
        err = anthropic.AuthenticationError("nope", response=response, body=None)
        with pytest.raises(PDFExtractionError, match="ANTHROPIC_API_KEY"):
            VisionImageExtractor(client=FakeAnthropic(err)).extract([png])

    def test_missing_key_is_explained(self, png, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(PDFExtractionError, match="ANTHROPIC_API_KEY"):
            VisionImageExtractor().extract([png])

    def test_unreadable_file_is_a_clear_error(self, tmp_path):
        bad = tmp_path / "x.png"
        bad.write_bytes(b"nope")
        with pytest.raises(PDFExtractionError, match="could not open image"):
            _encode_for_api(bad)

    def test_huge_images_are_shrunk_and_unsupported_formats_converted(self, tmp_path):
        big = tmp_path / "big.bmp"
        Image.new("RGB", (6000, 3000), "white").save(big)
        media_type, data = _encode_for_api(big)
        assert media_type == "image/png"
        import io

        assert max(Image.open(io.BytesIO(base64.b64decode(data))).size) <= 2400


@pytest.fixture
def hosted(tmp_path):
    ws = Workspace(Store(tmp_path / "h.db"), WorkspacePaths(db=tmp_path / "h.db"), hosted=True)
    ws.update_settings({"semester_start": "2026-01-12", "llm_backend": "stub"})
    yield ws
    ws.store.close()


class TestHostedImports:
    def test_hosted_reads_images_with_vision_not_tesseract(self, hosted):
        assert hosted.image_reader() == "vision"

    def test_screenshot_flows_through_the_normal_pipeline(self, hosted, png):
        fake = VisionImageExtractor(client=FakeAnthropic("ECON 101 essay due Week 10 Friday"))
        result = hosted.import_images([png], label="shot.png", image_extractor=fake)
        assert result["found"] == 5 and result["added"] == 5  # the stub's tasks

    def test_an_all_blank_image_is_reported_not_imported(self, hosted, png):
        fake = VisionImageExtractor(client=FakeAnthropic(NO_TEXT))
        with pytest.raises(WorkspaceError, match="no readable text"):
            hosted.import_images([png], label="shot.png", image_extractor=fake)

    def test_missing_api_key_is_surfaced_in_the_ui_state(self, hosted, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert "ANTHROPIC_API_KEY" in hosted.image_reader_problem()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        assert hosted.image_reader_problem() is None

    def test_a_scanned_pdf_gets_actionable_advice(self, hosted, tmp_path):
        scanned = tmp_path / "scan.pdf"
        Image.new("RGB", (600, 800), "white").save(scanned, "PDF")
        with pytest.raises(WorkspaceError, match="upload photos or screenshots"):
            hosted.import_pdf(scanned, label="scan.pdf")

    def test_hosted_does_not_ask_for_ocr(self, hosted):
        assert hosted.pipeline_config().ocr_enabled is False


def test_pages_with_no_text_are_reported_on_the_result(tmp_path):
    ws = Workspace(Store(tmp_path / "x.db"), WorkspacePaths(db=tmp_path / "x.db"))
    ws.update_settings({"semester_start": "2026-01-12", "llm_backend": "stub"})
    pages = [
        PageText(1, "Problem Set 1 due Week 3 Friday", "text_layer"),
        PageText(2, "", "empty"),
        PageText(3, "", "empty"),
    ]
    result = analyze_pages(ws.pipeline_config(), pages, source_name="x.pdf")
    assert result.empty_pages == [2, 3]
    ws.store.close()
