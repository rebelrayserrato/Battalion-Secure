"""Deterministic document compare / redline (RAYAAAA-231 / P1b)."""

from review_engine.compare.redline import (
    ComparisonResult,
    RedlineSegment,
    Segment,
    compare_documents,
    deterministic_summary,
    summarize_comparison,
)

__all__ = [
    "ComparisonResult",
    "RedlineSegment",
    "Segment",
    "compare_documents",
    "deterministic_summary",
    "summarize_comparison",
]
