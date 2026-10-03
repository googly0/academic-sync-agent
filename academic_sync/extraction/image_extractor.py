"""Stage 1 for screenshots and photos: OCR each image into a ``PageText``.

Most deadlines never arrive as a syllabus PDF — they are on a slide, a
whiteboard, a course web page, a group chat. A screenshot of any of those
reduces to the same thing a scanned PDF page does, so this reuses the PDF
extractor's types and failure modes: one image is one "page", and stages 2–4
run on it unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

from .pdf_extractor import (
    DocumentText,
    OCRUnavailableError,
    PageText,
    PDFExtractionError,
    _tidy,
)

logger = logging.getLogger(__name__)

#: Screenshots narrower than this are upscaled before OCR. Tesseract is tuned
#: for ~300 DPI print; UI text at screen resolution is too small for it, and a
#: 2x upscale is the cheapest large accuracy win available.
UPSCALE_BELOW_WIDTH = 1600


class ImageTextExtractor:
    """OCR a set of images, one ``PageText`` per image, in the order given.

    Unlike PDF extraction there is no text layer to fall back from, so OCR
    being unavailable is always fatal here: a "successful" import of blank
    images would be a silent miss.
    """

    def __init__(self, *, ocr_language: str = "eng") -> None:
        self.ocr_language = ocr_language

    def extract(self, image_paths: Sequence[str | Path]) -> DocumentText:
        if not image_paths:
            raise PDFExtractionError("no images to read")
        try:
            import pytesseract
            from PIL import Image, ImageOps
        except ImportError as exc:
            raise OCRUnavailableError(
                "OCR dependencies missing (pytesseract / Pillow). "
                "Install them plus the `tesseract` system package."
            ) from exc

        document = DocumentText(path=Path(image_paths[0]))
        for number, raw_path in enumerate(image_paths, start=1):
            path = Path(raw_path)
            try:
                with Image.open(path) as opened:
                    image = _prepare(opened, ImageOps)
            except Exception as exc:
                raise PDFExtractionError(f"could not open image {path.name}: {exc}") from exc
            try:
                text = pytesseract.image_to_string(image, lang=self.ocr_language)
            except pytesseract.TesseractNotFoundError as exc:
                raise OCRUnavailableError(
                    "the `tesseract` binary is not installed (macOS: brew install tesseract)"
                ) from exc
            except Exception as exc:
                raise PDFExtractionError(f"OCR failed on {path.name}: {exc}") from exc

            text = _tidy(text)
            source = "ocr" if text.strip() else "empty"
            if source == "empty":
                logger.warning("no text recovered from image %s", path.name)
            document.pages.append(PageText(page_number=number, text=text, source=source))
        return document


def _prepare(image, ImageOps):  # type: ignore[no-untyped-def]
    """Normalise orientation, drop colour, and upscale small screenshots."""
    image = ImageOps.exif_transpose(image)  # phone photos arrive rotated
    image = image.convert("L")
    if image.width < UPSCALE_BELOW_WIDTH:
        from PIL import Image

        image = image.resize((image.width * 2, image.height * 2), Image.LANCZOS)
    return image


def ocr_available() -> Optional[str]:
    """``None`` when OCR works, else a short reason. Used by the UI."""
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
    except ImportError:
        return "pytesseract is not installed"
    except Exception:
        return "the tesseract binary is not installed"
    return None
