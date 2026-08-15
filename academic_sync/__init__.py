"""Autonomous Academic Management Agent.

A fault-tolerant, three-stage pipeline that turns messy syllabus PDFs into
Google Calendar events:

    1. ``extraction.pdf_extractor``  — PDF/OCR text extraction (local, cheap)
    2. ``extraction.llm``            — semantic extraction (LLM, model-agnostic)
    3. ``resolution.date_resolver``  — deterministic date arithmetic (no LLM)
    4. ``models.task``               — Pydantic validation / review gating
    5. ``calendar_sync``             — idempotent, resumable Google Calendar sync

Each stage is importable and testable in isolation; the orchestrator is the
only module that knows about all five.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
