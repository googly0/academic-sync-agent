"""Stage 2 — semantic extraction, behind a model-agnostic interface.

Import ``LLMExtractor`` to subclass; import ``create_extractor`` to use one.
Concrete backends are never imported directly by the pipeline.
"""

from .base import (
    ExtractionContext,
    LLMExtractionError,
    LLMExtractor,
    TextChunk,
)
from .registry import available_backends, create_extractor, register_backend

__all__ = [
    "LLMExtractor",
    "LLMExtractionError",
    "ExtractionContext",
    "TextChunk",
    "create_extractor",
    "register_backend",
    "available_backends",
]
