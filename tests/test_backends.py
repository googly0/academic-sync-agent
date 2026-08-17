"""Guards on the backend registry — specifically, who owns a default.

Companion to tests/test_prompts.py. That file guards prompt/schema drift; this
one guards the other defect this project has already been bitten by: a default
value written down in two places, where the outer copy silently overrides the
backend's own. ``--chunk-chars`` did exactly that, passing the CLI's 60000 into
OllamaExtractor and swamping its deliberately smaller 6000.

The rule: "flag not supplied" must reach the backend as *absence*, so the
backend's ``__init__`` default is the only default that exists.
"""

from __future__ import annotations

import inspect

from academic_sync.extraction.llm import create_extractor
from academic_sync.extraction.llm.base import LLMExtractor
from academic_sync.extraction.llm.ollama_extractor import OllamaExtractor


def _init_default(cls, parameter: str):
    return inspect.signature(cls.__init__).parameters[parameter].default


class TestChunkSizeDefaults:
    def test_omitted_chunk_chars_leaves_each_backend_on_its_own_default(self):
        """This is the regression the --chunk-chars fix was about."""
        assert create_extractor("stub").max_chunk_chars == _init_default(
            LLMExtractor, "max_chunk_chars"
        )
        assert create_extractor("ollama").max_chunk_chars == _init_default(
            OllamaExtractor, "max_chunk_chars"
        )

    def test_backends_do_not_share_one_chunk_size(self):
        """If these ever collapse to the same number the test above passes
        vacuously, so assert the difference the fix was protecting."""
        assert create_extractor("ollama").max_chunk_chars < create_extractor("stub").max_chunk_chars

    def test_an_explicit_chunk_size_still_wins(self):
        assert create_extractor("stub", max_chunk_chars=1234).max_chunk_chars == 1234
        assert create_extractor("ollama", max_chunk_chars=1234).max_chunk_chars == 1234

    def test_none_means_not_supplied_rather_than_zero(self):
        """The orchestrator passes ``max_chunk_chars=None`` when the flag is
        absent; that must not reach a backend as a value."""
        assert create_extractor("stub", max_chunk_chars=None).max_chunk_chars == _init_default(
            LLMExtractor, "max_chunk_chars"
        )
