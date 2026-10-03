"""Read course emails from Gmail and turn each one into a ``PageText``.

Instructors announce deadlines by email far more often than they revise a
syllabus. Each message becomes one "page" — headers plus plain-text body — and
then runs through the same stages 2–4 as a PDF. Nothing here interprets dates:
"due this Friday" is passed through verbatim and, because the resolver refuses
it, lands in the review inbox with the email's sent date shown beside it.

Access is ``gmail.readonly``: this module can list and read messages and
nothing else.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional

from ..extraction.pdf_extractor import PageText

logger = logging.getLogger(__name__)

#: A reasonable first pass at "emails that might contain a deadline". Editable
#: in the app's settings; Gmail's own search does the filtering server-side.
DEFAULT_QUERY = (
    "newer_than:30d "
    "(subject:(due OR deadline OR exam OR quiz OR midterm OR assignment OR homework "
    "OR project OR submission) OR from:instructure.com OR from:canvas)"
)

#: Bodies past this are almost always quoted reply chains or footers. Keeping
#: the head keeps the model call small and the announcement intact.
MAX_BODY_CHARS = 12_000


@dataclass(frozen=True)
class EmailDoc:
    message_id: str
    subject: str
    sender: str
    date: str
    body: str

    @property
    def label(self) -> str:
        return self.subject or "(no subject)"

    def to_page(self) -> PageText:
        """Headers first so the model sees who sent it and about what."""
        text = (
            f"From: {self.sender}\n"
            f"Subject: {self.subject}\n"
            f"Date: {self.date}\n\n"
            f"{self.body}"
        )
        return PageText(page_number=1, text=text, source="text_layer")


class GmailSource:
    """Lists and fetches messages through an authorised Gmail v1 service."""

    def __init__(self, service: Any) -> None:
        self.service = service

    def list_ids(self, query: str, *, max_results: int = 25) -> List[str]:
        response = (
            self.service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        return [m["id"] for m in response.get("messages") or []]

    def fetch(self, message_id: str) -> EmailDoc:
        message = (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        payload = message.get("payload") or {}
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers") or []}
        body = extract_body(payload) or message.get("snippet", "")
        return EmailDoc(
            message_id=message_id,
            subject=headers.get("subject", ""),
            sender=headers.get("from", ""),
            date=headers.get("date", ""),
            body=_squash(body)[:MAX_BODY_CHARS],
        )


def extract_body(payload: Dict[str, Any]) -> str:
    """Plain text from a Gmail message payload.

    Prefers a ``text/plain`` part anywhere in the MIME tree; falls back to
    ``text/html`` with tags stripped. Attachments are ignored.
    """
    parts = list(_walk(payload))
    for mime in ("text/plain", "text/html"):
        for part in parts:
            if part.get("mimeType") != mime or part.get("filename"):
                continue
            data = (part.get("body") or {}).get("data")
            if not data:
                continue
            text = _b64(data)
            return html_to_text(text) if mime == "text/html" else text
    return ""


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "section"}
    _SKIP = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: List[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _walk(part: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    yield part
    for child in part.get("parts") or []:
        yield from _walk(child)


def _b64(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")


def _squash(text: str) -> str:
    """Collapse runs of blank lines and trailing spaces; keep line structure."""
    text = text.replace("\r\n", "\n")
    text = re.sub(r"[ \t ]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_query(query: Optional[str]) -> str:
    return (query or "").strip() or DEFAULT_QUERY
