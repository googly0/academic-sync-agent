"""A second concrete backend: a local small model served by Ollama.

This file exists to prove the abstraction holds. It is ~90 lines, it implements
exactly one method, and **nothing else in the project changes** to use it:

    python main.py --pdf syllabus.pdf --semester-start 2026-01-12 \\
        --llm-backend ollama --model llama3.1:8b --dry-run

Adapting it to a HuggingFace ``pipeline``, llama.cpp, vLLM, or an internal
inference endpoint is the same shape — subclass, implement
``_extract_from_text``, register.

Uses ``urllib`` from the standard library rather than adding an HTTP dependency
for what is essentially one POST.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, List

from ...models.task import RawExtractedTask, SyllabusExtraction
from .base import ExtractionContext, LLMExtractionError, LLMExtractor
from .prompts import EXTRACTION_SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "llama3.1:8b"


class OllamaExtractor(LLMExtractor):
    """Semantic extraction via a locally served model.

    Ollama's ``format`` parameter accepts a JSON Schema and constrains decoding
    to it, which is the local equivalent of structured outputs. We hand it the
    same Pydantic-derived schema the Anthropic backend uses, so both backends
    are held to an identical contract.

    Args:
        model: Ollama model tag, e.g. ``llama3.1:8b`` or ``qwen2.5:14b``.
        host: Base URL of the Ollama server.
        timeout: Seconds to wait. Local models on CPU are slow; the default is
            deliberately generous.
        max_chunk_chars: Smaller than the API default — small models have
            small context windows and degrade badly when crowded.
    """

    name = "ollama"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        timeout: float = 600.0,
        max_chunk_chars: int = 12_000,
    ) -> None:
        super().__init__(max_chunk_chars=max_chunk_chars)
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout

    def _extract_from_text(
        self, text: str, context: ExtractionContext
    ) -> List[RawExtractedTask]:
        payload = {
            "model": self.model,
            "stream": False,
            "format": SyllabusExtraction.model_json_schema(),
            # Local models drift far more than frontier ones; a low temperature
            # keeps verbatim copying of date phrases actually verbatim.
            "options": {"temperature": 0.0},
            "messages": [
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_user_prompt(
                        text,
                        source_name=context.source_name,
                        chunk_index=context.chunk_index,
                        chunk_count=context.chunk_count,
                        page_range=context.page_range,
                        course_hint=context.course_hint,
                    ),
                },
            ],
        }

        body = self._post("/api/chat", payload)
        content = (body.get("message") or {}).get("content")
        if not content:
            raise LLMExtractionError(
                f"chunk {context.chunk_index}: Ollama returned an empty message"
            )

        try:
            return list(SyllabusExtraction(**json.loads(content)).tasks)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise LLMExtractionError(
                f"chunk {context.chunk_index}: local model returned unusable JSON: {exc}"
            ) from exc

    def _post(self, path: str, payload: dict) -> Any:
        request = urllib.request.Request(
            f"{self.host}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LLMExtractionError(
                f"Ollama returned HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:400]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMExtractionError(
                f"could not reach Ollama at {self.host} ({exc.reason}); is `ollama serve` running?"
            ) from exc
