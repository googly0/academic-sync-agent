"""Tests for the non-PDF sources: Gmail parsing and screenshot OCR."""

from __future__ import annotations

import base64
import shutil

import pytest

from academic_sync.sources.gmail import (
    DEFAULT_QUERY,
    EmailDoc,
    GmailSource,
    extract_body,
    html_to_text,
    parse_query,
)


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class TestGmailBodies:
    def test_prefers_plain_text_anywhere_in_the_tree(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/html", "body": {"data": b64("<p>HTML</p>")}},
                        {"mimeType": "text/plain", "body": {"data": b64("PS3 due Oct 10")}},
                    ],
                },
                {"mimeType": "text/plain", "filename": "notes.txt", "body": {"attachmentId": "a"}},
            ],
        }
        assert extract_body(payload) == "PS3 due Oct 10"

    def test_falls_back_to_html_without_scripts_or_styles(self):
        html = "<style>p{}</style><p>Quiz 2</p><p>due <b>Week 5 Friday</b></p><script>x()</script>"
        text = html_to_text(html)
        assert "Quiz 2" in text and "due Week 5 Friday" in text
        assert "x()" not in text and "p{}" not in text

    def test_email_page_leads_with_headers(self):
        page = EmailDoc("m", "PS3", "Prof <p@u.edu>", "Mon, 5 Oct 2026", "due Friday").to_page()
        assert page.text.startswith("From: Prof <p@u.edu>\nSubject: PS3\nDate: Mon, 5 Oct 2026")

    def test_fetch_reads_headers_and_body(self):
        message = {
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": "Midterm"}, {"name": "From", "value": "p@u.edu"}],
                "body": {"data": b64("Midterm is Oct 20\r\n\r\n\r\n\r\nthanks")},
            }
        }
        email = GmailSource(FakeGmailService(message)).fetch("m1")
        assert email.subject == "Midterm" and email.sender == "p@u.edu"
        assert email.body == "Midterm is Oct 20\n\nthanks"

    def test_blank_query_uses_the_default(self):
        assert parse_query("  ") == DEFAULT_QUERY
        assert parse_query("from:prof") == "from:prof"


class FakeGmailService:
    def __init__(self, message):
        self.message = message

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, **kwargs):
        return self

    def execute(self):
        return self.message


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_screenshot_ocr_reads_rendered_text(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    from academic_sync.extraction.image_extractor import ImageTextExtractor

    image = Image.new("RGB", (900, 120), "white")
    try:
        font = ImageFont.load_default(size=36)
    except TypeError:  # Pillow < 10.1 has no sized default font
        font = ImageFont.load_default()
    ImageDraw.Draw(image).text((20, 30), "Problem Set 3 due Week 5 Friday", fill="black", font=font)
    path = tmp_path / "shot.png"
    image.save(path)

    document = ImageTextExtractor().extract([path])
    assert document.pages[0].source == "ocr"
    assert "Week 5 Friday" in document.pages[0].text


def test_unreadable_image_is_a_clear_error(tmp_path):
    from academic_sync.extraction.image_extractor import ImageTextExtractor
    from academic_sync.extraction.pdf_extractor import PDFExtractionError

    bogus = tmp_path / "x.png"
    bogus.write_bytes(b"not an image")
    with pytest.raises(PDFExtractionError, match="could not open image"):
        ImageTextExtractor().extract([bogus])
