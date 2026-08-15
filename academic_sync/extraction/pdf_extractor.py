"""Stage 1 — PDF text extraction, local and cheap.

Strategy: try the PDF's own text layer first (PyPDF2, milliseconds, free). Only
pages that come back essentially empty — scans, photocopies, image-only exports
— get rasterised and pushed through Tesseract OCR, which is orders of magnitude
slower and imperfect.

This per-page fallback matters in practice: real syllabi are frequently a
digital cover page followed by a scanned schedule table, and a document-level
"is this a scan?" check would either OCR everything (slow) or nothing (loses
the table).

External binaries
-----------------
OCR needs two things that pip cannot install:

* ``tesseract`` — the OCR engine itself (``brew install tesseract``)
* ``poppler``  — provides ``pdftoppm``, used by pdf2image
  (``brew install poppler``)

If they are missing, the default behaviour is to **fail loudly** rather than
hand the LLM a document with silently blank pages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class PDFExtractionError(RuntimeError):
    """The PDF could not be read, or a page could not be recovered."""


class OCRUnavailableError(PDFExtractionError):
    """OCR was required for at least one page but is not installed/working."""


@dataclass
class PageText:
    """Text recovered from a single PDF page."""

    page_number: int  # 1-indexed, matching what a human sees in a PDF reader
    text: str
    source: str  # "text_layer" | "ocr" | "empty"

    @property
    def char_count(self) -> int:
        return len(self.text.strip())


@dataclass
class DocumentText:
    """All recovered text for one document, plus extraction provenance."""

    path: Path
    pages: List[PageText] = field(default_factory=list)

    @property
    def ocr_page_numbers(self) -> List[int]:
        """Pages that needed OCR — worth logging, since OCR text is noisier
        and is the usual source of downstream date-resolution failures."""
        return [p.page_number for p in self.pages if p.source == "ocr"]

    @property
    def empty_page_numbers(self) -> List[int]:
        return [p.page_number for p in self.pages if p.source == "empty"]

    def full_text(self) -> str:
        """Whole document with explicit page markers.

        The markers are not decoration: they let the LLM attribute each task to
        a page number, which is what makes ``needs_review.json`` actionable.
        """
        return "\n\n".join(
            f"--- PAGE {p.page_number} ---\n{p.text.strip()}"
            for p in self.pages
            if p.text.strip()
        )


class PDFTextExtractor:
    """Extracts per-page text with an automatic OCR fallback.

    Args:
        min_chars_per_page: A page whose text layer yields fewer than this many
            non-whitespace characters is assumed to be an image and is sent to
            OCR. 40 is low enough not to trigger on a genuinely sparse page and
            high enough to catch scans that leak a stray header.
        ocr_enabled: Set ``False`` to skip OCR entirely (fast, but scanned
            pages come back empty and are reported as such).
        ocr_dpi: Rasterisation DPI. 300 is the usual accuracy/speed sweet spot
            for Tesseract on text documents.
        ocr_language: Tesseract language pack code.
        poppler_path: Explicit path to poppler binaries, for platforms where
            they are not on ``PATH``.
        on_ocr_error: ``"raise"`` (default) aborts the run if OCR is needed but
            unavailable; ``"warn"`` logs and leaves the page empty. Default is
            strict because a silently missing page means silently missing
            deadlines.
    """

    def __init__(
        self,
        *,
        min_chars_per_page: int = 40,
        ocr_enabled: bool = True,
        ocr_dpi: int = 300,
        ocr_language: str = "eng",
        poppler_path: Optional[str] = None,
        on_ocr_error: str = "raise",
    ) -> None:
        if on_ocr_error not in ("raise", "warn"):
            raise ValueError("on_ocr_error must be 'raise' or 'warn'")
        self.min_chars_per_page = min_chars_per_page
        self.ocr_enabled = ocr_enabled
        self.ocr_dpi = ocr_dpi
        self.ocr_language = ocr_language
        self.poppler_path = poppler_path
        self.on_ocr_error = on_ocr_error

    # -- public API --------------------------------------------------------

    def extract(self, pdf_path: str | Path) -> DocumentText:
        """Extract text from every page of ``pdf_path``."""
        path = Path(pdf_path).expanduser().resolve()
        if not path.is_file():
            raise PDFExtractionError(f"PDF not found: {path}")

        document = DocumentText(path=path)
        for page_number, layer_text in enumerate(self._read_text_layer(path), start=1):
            cleaned = _tidy(layer_text)

            if len(cleaned) >= self.min_chars_per_page:
                document.pages.append(
                    PageText(page_number=page_number, text=cleaned, source="text_layer")
                )
                continue

            if not self.ocr_enabled:
                logger.warning(
                    "page %d has almost no text layer (%d chars) and OCR is disabled",
                    page_number,
                    len(cleaned),
                )
                document.pages.append(
                    PageText(
                        page_number=page_number,
                        text=cleaned,
                        source="text_layer" if cleaned else "empty",
                    )
                )
                continue

            logger.info("page %d looks scanned (%d chars); running OCR", page_number, len(cleaned))
            ocr_text = self._ocr_page(path, page_number)
            if ocr_text is None:
                document.pages.append(
                    PageText(page_number=page_number, text=cleaned, source="empty")
                )
            else:
                document.pages.append(
                    PageText(page_number=page_number, text=_tidy(ocr_text), source="ocr")
                )

        if not any(p.text.strip() for p in document.pages):
            raise PDFExtractionError(
                f"no text recovered from {path.name} — the file may be corrupt, "
                "encrypted, or an image-only scan with OCR unavailable"
            )
        return document

    # -- internals ---------------------------------------------------------

    def _read_text_layer(self, path: Path) -> List[str]:
        """Return raw per-page text from the PDF's own text layer.

        A page that raises during extraction yields ``""`` rather than killing
        the run — one malformed page should not cost the user the other 11.
        """
        try:
            from PyPDF2 import PdfReader
        except ImportError as exc:  # pragma: no cover - environment problem
            raise PDFExtractionError(
                "PyPDF2 is not installed; run `pip install -r requirements.txt`"
            ) from exc

        try:
            reader = PdfReader(str(path))
        except Exception as exc:
            raise PDFExtractionError(f"could not open {path.name}: {exc}") from exc

        # Encrypted PDFs: try the empty password, which unlocks the common
        # "owner password only" case. Anything else is a hard stop.
        if getattr(reader, "is_encrypted", False):
            try:
                if reader.decrypt("") == 0:
                    raise PDFExtractionError(f"{path.name} is password-protected")
            except PDFExtractionError:
                raise
            except Exception as exc:
                raise PDFExtractionError(f"{path.name} is password-protected: {exc}") from exc

        pages: List[str] = []
        for index, page in enumerate(reader.pages, start=1):
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:
                logger.warning("text-layer extraction failed on page %d: %s", index, exc)
                pages.append("")
        return pages

    def _ocr_page(self, path: Path, page_number: int) -> Optional[str]:
        """OCR a single page. Returns ``None`` when OCR is unavailable and
        ``on_ocr_error='warn'``; raises when it is ``'raise'``.

        Imports live inside the function so that a dry run on a digital-only
        PDF never requires Tesseract to be installed.
        """
        try:
            import pytesseract
            from pdf2image import convert_from_path
        except ImportError as exc:
            return self._handle_ocr_failure(
                page_number,
                OCRUnavailableError(
                    "OCR dependencies missing (pytesseract / pdf2image). "
                    "Install them plus the `tesseract` and `poppler` system packages."
                ),
                exc,
            )

        try:
            # Render only the page we need — converting a 40-page scan to
            # images just to read page 7 is a real cost.
            images = convert_from_path(
                str(path),
                dpi=self.ocr_dpi,
                first_page=page_number,
                last_page=page_number,
                poppler_path=self.poppler_path,
            )
            if not images:
                raise OCRUnavailableError(f"poppler produced no image for page {page_number}")
            return pytesseract.image_to_string(images[0], lang=self.ocr_language)
        except Exception as exc:
            return self._handle_ocr_failure(
                page_number,
                OCRUnavailableError(f"OCR failed on page {page_number}: {exc}"),
                exc,
            )

    def _handle_ocr_failure(
        self, page_number: int, error: OCRUnavailableError, cause: Exception
    ) -> None:
        if self.on_ocr_error == "raise":
            raise error from cause
        logger.error("page %d: %s (continuing with empty text)", page_number, error)
        return None


def _tidy(text: str) -> str:
    """Normalise whitespace without destroying line structure.

    Line breaks are preserved deliberately: syllabus schedules are tables, and
    collapsing them to one line makes it far harder for the LLM to associate a
    task with its date.
    """
    lines = [line.rstrip() for line in (text or "").splitlines()]
    # Drop runs of blank lines but keep single blank lines as paragraph breaks.
    cleaned: List[str] = []
    for line in lines:
        if not line.strip() and (not cleaned or not cleaned[-1].strip()):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()
