"""Tests for stage 1 — specifically, how a page that yields nothing is reported.

The pipeline's whole premise is that a missing deadline must be visible. Stage 1
signals that through ``DocumentText.empty_page_numbers``, which the orchestrator
turns into "deadlines on them will be missed". A page that slips out of that list
is a silently lost page, so the classification of ``PageText.source`` is load
bearing rather than cosmetic.

No PDF, Tesseract, or poppler is needed: the text-layer read and the OCR call are
the two seams, and both are overridden here.
"""

from __future__ import annotations

import pytest

from academic_sync.extraction.pdf_extractor import PDFExtractionError, PDFTextExtractor


class _FakeExtractor(PDFTextExtractor):
    """Scripts the two external calls stage 1 makes."""

    def __init__(self, layer_pages, ocr_results, **kwargs):
        super().__init__(**kwargs)
        self._layer_pages = layer_pages
        self._ocr_results = ocr_results
        self.ocr_calls = []

    def _read_text_layer(self, path):
        return list(self._layer_pages)

    def _ocr_page(self, path, page_number):
        self.ocr_calls.append(page_number)
        return self._ocr_results.get(page_number)


REAL_PAGE = "Problem Set 1 (10%) is due Week 3 Friday. Midterm Exam in Week 7.\n"


class TestEmptyPageReporting:
    def test_ocr_that_recovers_nothing_is_reported_as_empty(self, tmp_path):
        """A blank/failed scan must not be counted as a successful OCR page.

        Recording it as "ocr" keeps it out of ``empty_page_numbers``, so the
        run reports "1 page via OCR" and never warns that the page contributed
        nothing — exactly the silent page loss this pipeline exists to prevent.
        """
        pdf = tmp_path / "syllabus.pdf"
        pdf.write_bytes(b"%PDF-1.4")

        extractor = _FakeExtractor(
            layer_pages=["", REAL_PAGE],
            # Tesseract returns whitespace/form-feeds for a page with no glyphs.
            ocr_results={1: "   \n\f  "},
        )
        document = extractor.extract(pdf)

        assert extractor.ocr_calls == [1]
        assert document.empty_page_numbers == [1]
        assert document.ocr_page_numbers == []

    def test_ocr_that_recovers_text_is_reported_as_ocr(self, tmp_path):
        pdf = tmp_path / "syllabus.pdf"
        pdf.write_bytes(b"%PDF-1.4")

        extractor = _FakeExtractor(
            layer_pages=["", REAL_PAGE],
            ocr_results={1: "Final Project (40%) due Week 14 Friday.\n"},
        )
        document = extractor.extract(pdf)

        assert document.ocr_page_numbers == [1]
        assert document.empty_page_numbers == []
        assert "Final Project" in document.pages[0].text

    def test_a_document_that_recovers_nothing_at_all_still_fails_loudly(self, tmp_path):
        pdf = tmp_path / "syllabus.pdf"
        pdf.write_bytes(b"%PDF-1.4")

        extractor = _FakeExtractor(layer_pages=["", ""], ocr_results={1: "", 2: ""})
        with pytest.raises(PDFExtractionError):
            extractor.extract(pdf)
