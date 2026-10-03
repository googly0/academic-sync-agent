"""Stage 1 for screenshots and photos: OCR each image into a ``PageText``.

Most deadlines never arrive as a syllabus PDF — they are on a slide, a
whiteboard, a course web page, a group chat. A screenshot of any of those
reduces to the same thing a scanned PDF page does, so this reuses the PDF
extractor's types and failure modes: one image is one "page", and stages 2–4
run on it unchanged.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional, Sequence

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


# ---------------------------------------------------------------------------
# Reading images with Claude instead of Tesseract
# ---------------------------------------------------------------------------

#: Used when Tesseract is unavailable — chiefly on Vercel, which cannot run
#: native binaries. Transcription is mechanical, so this is a mid-tier model;
#: the judgement-heavy extraction that follows uses the configured backend.
DEFAULT_VISION_MODEL = "claude-sonnet-5-5"

#: Longest edge sent to the API. Screenshots larger than this gain no accuracy
#: and risk the request-size limit.
MAX_EDGE_PX = 2400
MAX_IMAGE_BYTES = 4_500_000

NO_TEXT = "NO_TEXT"

TRANSCRIBE_PROMPT = (
    "Transcribe all text in this image exactly as written, keeping the line breaks "
    "and reading order. It may be a slide, a course web page, a chat, an email, or "
    "a photo of a whiteboard or printed schedule. Copy dates, times and numbers "
    "character for character: do not correct, reformat, translate or interpret "
    "them, and do not add anything that is not visible. If part of the text is "
    "illegible, write [illegible] in its place. If the image contains no text, "
    f"reply with exactly {NO_TEXT}."
)


class VisionImageExtractor:
    """Same contract as :class:`ImageTextExtractor`, backed by Claude's vision.

    One image is one ``PageText`` and everything downstream is unchanged. The
    model is asked only to *transcribe*; it is told not to interpret dates, so
    the verbatim-copy rule that protects the date resolver holds from this
    stage onward.

    Args:
        client: an ``anthropic.Anthropic`` instance; built from the environment
            when omitted. Injectable so tests need no network.
    """

    def __init__(self, *, client: Any = None, model: str = DEFAULT_VISION_MODEL) -> None:
        self._client = client
        self.model = model

    def extract(self, image_paths: Sequence[str | Path]) -> DocumentText:
        if not image_paths:
            raise PDFExtractionError("no images to read")
        client = self._client or self._build_client()

        document = DocumentText(path=Path(image_paths[0]))
        for number, raw_path in enumerate(image_paths, start=1):
            path = Path(raw_path)
            media_type, data = _encode_for_api(path)
            text = self._transcribe(client, media_type, data, path.name)
            text = _tidy(text)
            source = "ocr" if text.strip() else "empty"
            if source == "empty":
                logger.warning("no text recovered from image %s", path.name)
            document.pages.append(PageText(page_number=number, text=text, source=source))
        return document

    def _transcribe(self, client: Any, media_type: str, data: str, name: str) -> str:
        import anthropic

        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=4000,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": media_type, "data": data},
                            },
                            {"type": "text", "text": TRANSCRIBE_PROMPT},
                        ],
                    }
                ],
            )
        except anthropic.AuthenticationError as exc:
            raise PDFExtractionError(
                "reading images needs a valid ANTHROPIC_API_KEY (the server rejected it)"
            ) from exc
        except anthropic.APIError as exc:
            raise PDFExtractionError(f"could not read {name}: {exc}") from exc

        if getattr(response, "stop_reason", None) in ("max_tokens", "refusal"):
            raise PDFExtractionError(
                f"could not read {name}: the model's reply was "
                f"{'cut off' if response.stop_reason == 'max_tokens' else 'declined'}"
            )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()
        return "" if text == NO_TEXT else text

    @staticmethod
    def _build_client() -> Any:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - environment problem
            raise PDFExtractionError("the `anthropic` package is not installed") from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise PDFExtractionError(
                "reading images needs ANTHROPIC_API_KEY — set it in the environment"
            )
        return anthropic.Anthropic(max_retries=3, timeout=120.0)


def _encode_for_api(path: Path) -> tuple[str, str]:
    """Normalise an image to something the API accepts: PNG (or JPEG if huge)."""
    import base64
    import io

    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - environment problem
        raise OCRUnavailableError("Pillow is not installed") from exc
    try:
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
    except Exception as exc:
        raise PDFExtractionError(f"could not open image {path.name}: {exc}") from exc

    if max(image.size) > MAX_EDGE_PX:
        image.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX), Image.LANCZOS)

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    media_type = "image/png"
    if buffer.tell() > MAX_IMAGE_BYTES:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=85)
        media_type = "image/jpeg"
    return media_type, base64.standard_b64encode(buffer.getvalue()).decode("ascii")
