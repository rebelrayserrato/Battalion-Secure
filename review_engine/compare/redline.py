"""Deterministic document compare / redline between two versions (RAYAAAA-231 / P1b).

Phase 1 of the RAYAAAA-229 document-intelligence roadmap (extraction / evidence
module). Given two documents — or two versions of one document — that already
live in the same Task, this produces a structured redline: added / removed /
changed / unchanged segments over the existing ``SourceChunk`` model, so every
segment stays anchored to a ``SRC-`` source reference.

Everything here is deterministic and LOCAL. The diff itself uses only the
standard library ``difflib`` — no model, no network, same output for the same
input. An OPTIONAL plain-language summary of the diff can be drafted by the
existing bounded local-Ollama connector; it degrades gracefully to a
deterministic textual summary when the model is unavailable and never invents
facts. Consistent with the current Chroma + local sentence-transformers + local
Ollama posture; no external API, no egress.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Optional, Sequence

from review_engine.extraction.models import SourceChunk

# Imported lazily-friendly: the type is only used for the optional summary and
# the connector is always injected by the caller, so importing it here keeps the
# "reuse the existing bounded connector" contract without adding a hard runtime
# dependency on Ollama being reachable.
from review_engine.llm_connectors.ollama import OllamaConnector


@dataclass(frozen=True)
class Segment:
    """One comparable unit of text, anchored to the chunk it came from."""

    text: str
    source_ref: str
    citation: str
    document_name: str


@dataclass(frozen=True)
class RedlineSegment:
    """A single added / removed / changed / unchanged entry in the redline.

    ``base`` is the left/earlier version, ``compare`` is the right/later one.
    A ``removed`` entry has base text only; ``added`` has compare text only;
    ``changed`` and ``unchanged`` carry both sides. Source references are kept
    per side so the UI can anchor each entry back to its evidence.
    """

    kind: str  # "added" | "removed" | "changed" | "unchanged"
    base_text: str
    compare_text: str
    base_source_refs: tuple[str, ...]
    compare_source_refs: tuple[str, ...]
    base_citations: tuple[str, ...]
    compare_citations: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "base_text": self.base_text,
            "compare_text": self.compare_text,
            "base_source_refs": list(self.base_source_refs),
            "compare_source_refs": list(self.compare_source_refs),
            "base_citations": list(self.base_citations),
            "compare_citations": list(self.compare_citations),
        }


@dataclass(frozen=True)
class ComparisonResult:
    base_document: str
    compare_document: str
    segments: tuple[RedlineSegment, ...]

    @property
    def counts(self) -> dict:
        totals = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
        for segment in self.segments:
            totals[segment.kind] = totals.get(segment.kind, 0) + 1
        return totals

    @property
    def changed_segments(self) -> tuple[RedlineSegment, ...]:
        """Only the entries that actually differ (added / removed / changed)."""
        return tuple(s for s in self.segments if s.kind != "unchanged")

    @property
    def has_changes(self) -> bool:
        return any(s.kind != "unchanged" for s in self.segments)

    def to_dict(self) -> dict:
        return {
            "base_document": self.base_document,
            "compare_document": self.compare_document,
            "counts": self.counts,
            "segments": [s.to_dict() for s in self.segments],
        }


# Sentence-ish boundary: split after . ! ? ; : followed by whitespace. Kept
# simple and deterministic on purpose — this is a redline, not linguistics.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;:])\s+")


def _normalize(text: str) -> str:
    """Matching key: whitespace-collapsed, lower-cased. Display text is kept
    separately, so formatting-only differences do not show up as changes."""
    return re.sub(r"\s+", " ", text).strip().lower()


def _segment_chunks(chunks: Sequence[SourceChunk]) -> list[Segment]:
    """Split each chunk into sentence/line-level comparable units.

    Anchoring stays at chunk granularity: every unit carries its owning chunk's
    source reference and citation, so a redline entry can always be traced back
    to a ``SRC-`` id. Empty / whitespace-only units are dropped.
    """
    segments: list[Segment] = []
    for chunk in chunks:
        pieces: list[str] = []
        for line in chunk.text.splitlines():
            line = line.strip()
            if not line:
                continue
            pieces.extend(p for p in _SENTENCE_SPLIT.split(line) if p.strip())
        if not pieces:
            stripped = chunk.text.strip()
            if stripped:
                pieces = [stripped]
        for piece in pieces:
            segments.append(
                Segment(
                    text=piece.strip(),
                    source_ref=chunk.source_ref,
                    citation=chunk.citation,
                    document_name=chunk.document_name,
                )
            )
    return segments


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    """Order-preserving de-duplication (several units can share one chunk)."""
    seen: dict[str, None] = {}
    for value in values:
        if value not in seen:
            seen[value] = None
    return tuple(seen)


def _join(segments: Sequence[Segment]) -> str:
    return "\n".join(s.text for s in segments)


def _refs(segments: Sequence[Segment]) -> tuple[str, ...]:
    return _dedupe(s.source_ref for s in segments)


def _citations(segments: Sequence[Segment]) -> tuple[str, ...]:
    return _dedupe(s.citation for s in segments)


def compare_documents(
    base_document: str,
    base_chunks: Sequence[SourceChunk],
    compare_document: str,
    compare_chunks: Sequence[SourceChunk],
    *,
    include_unchanged: bool = False,
) -> ComparisonResult:
    """Diff two documents' chunk sets into a structured redline.

    Deterministic: uses ``difflib.SequenceMatcher`` over normalized
    sentence-level units. ``autojunk`` is off so long, repetitive contracts are
    not silently under-matched.
    """
    base_segments = _segment_chunks(base_chunks)
    compare_segments = _segment_chunks(compare_chunks)
    matcher = SequenceMatcher(
        a=[_normalize(s.text) for s in base_segments],
        b=[_normalize(s.text) for s in compare_segments],
        autojunk=False,
    )
    result: list[RedlineSegment] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        base_block = base_segments[i1:i2]
        compare_block = compare_segments[j1:j2]
        if tag == "equal":
            if include_unchanged:
                result.append(
                    RedlineSegment(
                        kind="unchanged",
                        base_text=_join(base_block),
                        compare_text=_join(compare_block),
                        base_source_refs=_refs(base_block),
                        compare_source_refs=_refs(compare_block),
                        base_citations=_citations(base_block),
                        compare_citations=_citations(compare_block),
                    )
                )
        elif tag == "delete":
            result.append(
                RedlineSegment(
                    kind="removed",
                    base_text=_join(base_block),
                    compare_text="",
                    base_source_refs=_refs(base_block),
                    compare_source_refs=(),
                    base_citations=_citations(base_block),
                    compare_citations=(),
                )
            )
        elif tag == "insert":
            result.append(
                RedlineSegment(
                    kind="added",
                    base_text="",
                    compare_text=_join(compare_block),
                    base_source_refs=(),
                    compare_source_refs=_refs(compare_block),
                    base_citations=(),
                    compare_citations=_citations(compare_block),
                )
            )
        elif tag == "replace":
            result.append(
                RedlineSegment(
                    kind="changed",
                    base_text=_join(base_block),
                    compare_text=_join(compare_block),
                    base_source_refs=_refs(base_block),
                    compare_source_refs=_refs(compare_block),
                    base_citations=_citations(base_block),
                    compare_citations=_citations(compare_block),
                )
            )
    return ComparisonResult(
        base_document=base_document,
        compare_document=compare_document,
        segments=tuple(result),
    )


def deterministic_summary(result: ComparisonResult) -> str:
    """Plain, model-free summary of the diff — always available."""
    if not result.has_changes:
        return (
            f"No differences found between '{result.base_document}' and "
            f"'{result.compare_document}'."
        )
    counts = result.counts
    return (
        f"Comparing '{result.base_document}' -> '{result.compare_document}': "
        f"{counts['added']} added, {counts['removed']} removed, "
        f"{counts['changed']} changed segment(s). Requires human review."
    )


def _diff_lines_for_prompt(result: ComparisonResult, limit: int = 60) -> str:
    """Compact, source-anchored diff for the optional local-model summary."""
    lines: list[str] = []
    for segment in result.changed_segments:
        if segment.kind == "added":
            lines.append(
                f"[ADDED {', '.join(segment.compare_source_refs)}] {segment.compare_text}"
            )
        elif segment.kind == "removed":
            lines.append(
                f"[REMOVED {', '.join(segment.base_source_refs)}] {segment.base_text}"
            )
        else:  # changed
            lines.append(
                f"[CHANGED {', '.join(segment.base_source_refs)} -> "
                f"{', '.join(segment.compare_source_refs)}] "
                f"FROM: {segment.base_text} | TO: {segment.compare_text}"
            )
        if len(lines) >= limit:
            lines.append("... (diff truncated)")
            break
    return "\n".join(lines)


def summarize_comparison(
    result: ComparisonResult, connector: Optional[OllamaConnector] = None
) -> str:
    """Optional plain-language summary of the diff.

    Reuses the existing bounded local-Ollama connector. If no connector is
    supplied, the local model is unreachable, or the call fails, returns the
    deterministic summary instead — never an external call, never a fabricated
    diff.
    """
    if not result.has_changes:
        return deterministic_summary(result)
    if connector is None or not connector.available():
        return deterministic_summary(result)
    prompt = (
        "You are a document-comparison assistant. Summarize, in plain language, "
        "the differences between two versions of a document using ONLY the diff "
        "lines below. Do not add facts, do not draw legal conclusions, do not "
        "state that fraud or a breach occurred. Preserve the SRC- source-"
        "reference IDs. End with 'Requires human review.'\n\n"
        f"BASE VERSION: {result.base_document}\n"
        f"COMPARED VERSION: {result.compare_document}\n\n"
        f"DIFF:\n{_diff_lines_for_prompt(result)}"
    )
    try:
        drafted = connector.generate(prompt).strip()
    except Exception:
        return deterministic_summary(result)
    return drafted or deterministic_summary(result)
