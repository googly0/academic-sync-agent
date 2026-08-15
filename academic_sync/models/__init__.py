"""Pydantic models shared across pipeline stages."""

from .task import (
    REVIEW_CONTRADICTION,
    REVIEW_MISSING_FIELDS,
    REVIEW_UNRESOLVED_DATE,
    AcademicTask,
    RawExtractedTask,
    SyllabusExtraction,
)

__all__ = [
    "AcademicTask",
    "RawExtractedTask",
    "SyllabusExtraction",
    "REVIEW_CONTRADICTION",
    "REVIEW_MISSING_FIELDS",
    "REVIEW_UNRESOLVED_DATE",
]
