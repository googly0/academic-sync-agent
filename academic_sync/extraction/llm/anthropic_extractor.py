"""Anthropic-backed implementation of :class:`LLMExtractor`.

Uses structured outputs so the model's reply is schema-validated by the API
before it ever reaches us — no regex scraping of prose, no "the model wrapped
the JSON in a code fence" class of bug.

Swapping this out: see ``ollama_extractor.py`` for a second concrete backend.
Nothing in this file is imported by the orchestrator.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, List, Optional

from ...models.task import RawExtractedTask, SyllabusExtraction
from .base import ExtractionContext, LLMExtractionError, LLMExtractor
from .prompts import EXTRACTION_SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)


def _describe_credential_source() -> str:
    """Describe where the credential came from, and its shape — never its value.

    Printing enough to diagnose a 401 without leaking a live secret into a
    terminal, a log file, or a pasted bug report.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return (
            "ANTHROPIC_API_KEY is not set, so the SDK used a stored profile or "
            "ANTHROPIC_AUTH_TOKEN."
        )
    if "..." in key:
        return (
            f"ANTHROPIC_API_KEY is set to a literal placeholder "
            f"({key[:7]}… contains '...'), not a real key."
        )
    return (
        f"ANTHROPIC_API_KEY is set: {len(key)} characters, starts with {key[:7]!r}."
    )


#: Default model. Extraction from noisy OCR text with contradiction detection
#: is a judgement-heavy task, so this defaults to the most capable model rather
#: than the cheapest; override with ``--model`` if cost matters more.
DEFAULT_MODEL = "claude-opus-5"

#: Generous but bounded. Well under the streaming threshold, so the plain
#: non-streaming call cannot hit an HTTP timeout.
DEFAULT_MAX_TOKENS = 16_000


class AnthropicExtractor(LLMExtractor):
    """Semantic extraction via the Anthropic Messages API.

    Args:
        model: Model id. Defaults to ``claude-opus-5``.
        api_key: Explicit key. When omitted the SDK resolves credentials from
            the environment (``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN``,
            or an ``ant auth login`` profile), which is the preferred path.
        max_tokens: Output cap per chunk.
        effort: ``low`` | ``medium`` | ``high`` | ``xhigh`` | ``max``. Left at
            ``None`` (API default) unless you need to trade cost for care.
        max_retries: Passed to the SDK, which already retries 429/5xx and
            connection errors with exponential backoff — we do not re-implement
            that here.
        timeout: Per-request timeout in seconds.
    """

    name = "anthropic"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: Optional[str] = None,
        max_retries: int = 4,
        timeout: float = 600.0,
        max_chunk_chars: int = 60_000,
    ) -> None:
        super().__init__(max_chunk_chars=max_chunk_chars)
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self._client = self._build_client(api_key, max_retries, timeout)

    # -- construction ------------------------------------------------------

    @staticmethod
    def _build_client(api_key: Optional[str], max_retries: int, timeout: float) -> Any:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - environment problem
            raise LLMExtractionError(
                "the `anthropic` package is not installed; "
                "run `pip install -r requirements.txt`"
            ) from exc

        kwargs: dict = {"max_retries": max_retries, "timeout": timeout}
        # Only pass api_key when explicitly provided — an unset
        # ANTHROPIC_API_KEY does not mean there are no credentials, and
        # passing None explicitly would defeat the SDK's own resolution chain.
        if api_key or os.environ.get("ANTHROPIC_API_KEY"):
            kwargs["api_key"] = api_key or os.environ["ANTHROPIC_API_KEY"]
        return anthropic.Anthropic(**kwargs)

    # -- the one abstract method -------------------------------------------

    def _extract_from_text(
        self, text: str, context: ExtractionContext
    ) -> List[RawExtractedTask]:
        user_prompt = build_user_prompt(
            text,
            source_name=context.source_name,
            chunk_index=context.chunk_index,
            chunk_count=context.chunk_count,
            page_range=context.page_range,
            course_hint=context.course_hint,
        )

        request: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            # The system prompt is byte-identical across chunks and runs, so it
            # is the natural cache breakpoint. Volatile content (the syllabus
            # chunk) goes after it, in the user turn.
            "system": [
                {
                    "type": "text",
                    "text": EXTRACTION_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if self.effort:
            request["output_config"] = {"effort": self.effort}

        response = self._call(request)
        self._guard_stop_reason(response, context)
        return self._parse(response, context)

    # -- transport ---------------------------------------------------------

    def _call(self, request: dict) -> Any:
        """Issue the request, preferring the typed ``messages.parse`` helper.

        ``parse`` validates the response against the Pydantic model for us.
        Older SDK builds without it fall back to ``create`` plus an explicit
        JSON-schema ``output_config``; both paths end up schema-constrained.
        """
        import anthropic

        try:
            if hasattr(self._client.messages, "parse"):
                return self._client.messages.parse(
                    output_format=SyllabusExtraction, **request
                )
            request = dict(request)
            output_config = dict(request.pop("output_config", {}))
            output_config["format"] = {
                "type": "json_schema",
                "schema": SyllabusExtraction.model_json_schema(),
            }
            request["output_config"] = output_config
            return self._client.messages.create(**request)
        except anthropic.AuthenticationError as exc:
            # 401 is almost always a setup mistake, not a transient failure —
            # so say what to check rather than echoing the raw API payload.
            raise LLMExtractionError(
                "Anthropic rejected the credentials (HTTP 401).\n"
                f"  {_describe_credential_source()}\n"
                "  Check that the key is a real one from https://console.anthropic.com/settings/keys\n"
                "  (a real key is ~100+ characters; 'sk-ant-...' with literal dots is a placeholder).\n"
                "  Note: a set-but-invalid ANTHROPIC_API_KEY overrides any `ant auth login` "
                "profile — unset it to fall back to the profile."
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMExtractionError(
                f"Anthropic denied this request (HTTP 403): {exc.message}. "
                "The key may lack access to the requested model, or the workspace "
                f"may not have {self.model!r} enabled."
            ) from exc
        except anthropic.NotFoundError as exc:
            raise LLMExtractionError(
                f"Model {self.model!r} was not found (HTTP 404). Check --model; "
                "ids are exact strings with no date suffix."
            ) from exc
        except anthropic.APIStatusError as exc:
            # The SDK has already exhausted its retries for anything retryable.
            raise LLMExtractionError(
                f"Anthropic API error {exc.status_code}: {exc.message}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMExtractionError(f"could not reach the Anthropic API: {exc}") from exc

    # -- response handling -------------------------------------------------

    @staticmethod
    def _guard_stop_reason(response: Any, context: ExtractionContext) -> None:
        """Fail loudly on refusal and truncation.

        Both would otherwise surface as "this chunk contained no assignments",
        which is indistinguishable from a genuinely empty page — exactly the
        silent failure this pipeline is designed to eliminate.
        """
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            raise LLMExtractionError(
                f"chunk {context.chunk_index}: the model declined this request"
                + (f" (category: {category})" if category else "")
            )
        if stop_reason == "max_tokens":
            raise LLMExtractionError(
                f"chunk {context.chunk_index}: response hit max_tokens and is truncated; "
                "lower --chunk-chars or raise the token budget"
            )

    def _parse(self, response: Any, context: ExtractionContext) -> List[RawExtractedTask]:
        """Pull ``List[RawExtractedTask]`` out of either response shape."""
        parsed = getattr(response, "parsed_output", None)
        if isinstance(parsed, SyllabusExtraction):
            return list(parsed.tasks)

        # ``create`` path: structured outputs guarantee the first text block is
        # valid JSON matching the schema.
        text_block = next(
            (b.text for b in getattr(response, "content", []) if getattr(b, "type", None) == "text"),
            None,
        )
        if not text_block:
            raise LLMExtractionError(
                f"chunk {context.chunk_index}: response contained no text content"
            )
        try:
            return list(SyllabusExtraction(**json.loads(text_block)).tasks)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise LLMExtractionError(
                f"chunk {context.chunk_index}: could not parse the model response: {exc}"
            ) from exc
