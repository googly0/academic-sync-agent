"""Backend registry — the seam that keeps the orchestrator model-agnostic.

The orchestrator asks for a backend *by name* and never imports a concrete
class. Adding a backend is two lines here plus one new file.

Backends are imported lazily inside their factory so that, for example, a
`--llm-backend ollama` run does not require the `anthropic` package to be
installed, and vice versa.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from .base import LLMExtractor


def _anthropic_factory(**kwargs: Any) -> LLMExtractor:
    from .anthropic_extractor import AnthropicExtractor

    return AnthropicExtractor(**kwargs)


def _ollama_factory(**kwargs: Any) -> LLMExtractor:
    from .ollama_extractor import OllamaExtractor

    # The Ollama backend has no api_key/effort concept; drop those quietly so
    # the same CLI flags work across backends.
    kwargs.pop("api_key", None)
    kwargs.pop("effort", None)
    return OllamaExtractor(**kwargs)


def _stub_factory(**kwargs: Any) -> LLMExtractor:
    from .stub_extractor import StubExtractor

    # The stub ignores every model-ish option by design.
    return StubExtractor(max_chunk_chars=kwargs.get("max_chunk_chars", 60_000))


_REGISTRY: Dict[str, Callable[..., LLMExtractor]] = {
    "anthropic": _anthropic_factory,
    "ollama": _ollama_factory,
    "stub": _stub_factory,
}


def register_backend(name: str, factory: Callable[..., LLMExtractor]) -> None:
    """Register a new backend at runtime (plugins, tests, private models)."""
    if not name or name in _REGISTRY:
        raise ValueError(f"backend name {name!r} is empty or already registered")
    _REGISTRY[name] = factory


def available_backends() -> List[str]:
    """Names accepted by ``--llm-backend``."""
    return sorted(_REGISTRY)


def create_extractor(name: str, **kwargs: Any) -> LLMExtractor:
    """Instantiate a backend by name.

    ``kwargs`` are forwarded to the factory; each factory is responsible for
    ignoring options that do not apply to it, so the CLI can pass a uniform
    set of flags regardless of which backend is selected.
    """
    try:
        factory = _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown LLM backend {name!r}; available: {', '.join(available_backends())}"
        ) from None
    # Drop None values so factory defaults win over "flag not supplied".
    return factory(**{k: v for k, v in kwargs.items() if v is not None})
