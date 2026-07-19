"""Statutory-only extraction (RAYAAAA-274 P2, Counsel Condition B).

*Ingest statutory text ONLY.* Statutory / regulatory text produced by the U.S.
government is public domain (17 U.S.C. §105; *Georgia v. Public.Resource.Org*,
590 U.S. ___ (2020)). The **annotations, headnotes, notes-of-decisions, and
editorial apparatus** that commercial publishers (West / Thomson Reuters,
LexisNexis) layer over that free text are *copyrighted* and must never be
ingested — even when a nominally "official" state portal serves them alongside
the statute.

This module splits fetched law text into :class:`LawSegment` pieces classified as
``statutory`` or ``annotated``, keeps only the statutory pieces, and reports what
was dropped so the pipeline can flag statutory-vs-annotated in provenance. It is
conservative and fail-safe about the *annotation* side: a run of text under a
known annotation heading (or bearing a publisher marker) is treated as annotation
and excluded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Headings that introduce a NON-statutory, publisher/editorial block. Matched at
# the start of a line, case-insensitively. Everything from such a heading until
# the next statutory heading (or end of text) is treated as annotation.
_ANNOTATION_HEADINGS = (
    "notes of decisions",
    "annotations",
    "case notes",
    "editor's notes",
    "editors notes",
    "editorial notes",  # dropped as apparatus — see note after the tuple
    "historical and statutory notes",
    "history",
    "library references",
    "research references",
    "cross references",
    "law review",
    "law reviews",
    "commentary",
    "practice commentaries",
    "publisher's note",
    "publishers note",
    "west's",
    "westlaw",
    "lexisnexis",
    "lexis",
    "annotation",
)
# NB "editorial notes" is dropped too. Federal USC editorial notes (by the Office
# of Law Revision Counsel) are themselves public domain, but they are not the
# operative statutory text; "statutory text ONLY" (Counsel B) is the conservative
# floor, so we exclude them rather than risk pulling in adjacent copyrighted
# apparatus mislabelled under the same heading.

# Publisher fingerprints — if a segment carries one of these it is presumptively
# copyrighted apparatus, not statutory text.
_PUBLISHER_MARKERS = (
    "thomson reuters",
    "west publishing",
    "west group",
    "lexisnexis",
    "copyright ©",
    "© west",
    "all rights reserved",
)

_HEADING_RE = re.compile(r"^\s*([A-Za-z][A-Za-z'’ .]+?)\s*[:\-—]?\s*$")

# A block that STARTS with one of these is operative statutory / regulatory text
# (a new section). Seeing one ends any preceding annotation block — essential for
# real code text, where publisher "Notes" sit between consecutive sections.
_STATUTORY_START_RE = re.compile(
    r"^\s*(?:"
    r"§+\s*\d"                                   # "§ 552"
    r"|(?:section|sec\.)\s+\d"                   # "Section 2." / "Sec. 2"
    r"|\d+\s+(?:U\.?\s?S\.?\s?C\.?|C\.?\s?F\.?\s?R\.?)"  # "29 U.S.C." / "29 CFR"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LawSegment:
    text: str
    kind: str  # "statutory" | "annotated"
    reason: str = ""  # why an "annotated" segment was classified so


@dataclass(frozen=True)
class StatutoryExtraction:
    """Result of splitting a document into statutory vs. annotated segments."""

    statutory_text: str
    segments: tuple[LawSegment, ...] = field(default_factory=tuple)
    dropped_annotations: tuple[LawSegment, ...] = field(default_factory=tuple)

    @property
    def contained_annotations(self) -> bool:
        return bool(self.dropped_annotations)

    @property
    def content_type(self) -> str:
        """The Counsel-B provenance flag for the *ingested* material.

        Always ``statutory`` because only statutory segments are kept; the
        ``contained_annotations`` boolean records that some apparatus was found
        and stripped (surfaced separately in provenance / audit)."""
        return "statutory"


def _looks_like_heading(line: str) -> str | None:
    """Return the lowercased heading text if ``line`` is a short heading, else None."""
    stripped = line.strip()
    if not stripped or len(stripped) > 60:
        return None
    m = _HEADING_RE.match(stripped)
    if not m:
        return None
    return m.group(1).strip().lower()


def _is_annotation_heading(heading: str) -> bool:
    return any(heading == h or heading.startswith(h) for h in _ANNOTATION_HEADINGS)


def _has_publisher_marker(text: str) -> str:
    low = text.lower()
    for marker in _PUBLISHER_MARKERS:
        if marker in low:
            return marker
    return ""


def extract_statutory(raw_text: str) -> StatutoryExtraction:
    """Split ``raw_text`` into statutory vs. annotated segments; keep statutory.

    Blocks are delimited by blank lines. A block that begins under an annotation
    heading — or that carries a commercial-publisher fingerprint — is classified
    ``annotated`` and excluded. Everything else is treated as statutory text.
    """
    if not raw_text or not raw_text.strip():
        return StatutoryExtraction(statutory_text="", segments=(), dropped_annotations=())

    # Split into paragraph blocks on blank lines, preserving internal newlines.
    blocks = re.split(r"\n\s*\n", raw_text)
    segments: list[LawSegment] = []
    in_annotation = False

    for block in blocks:
        if not block.strip():
            continue
        stripped = block.strip()
        first_line = block.splitlines()[0] if block.splitlines() else ""

        # A new statutory section always ends any preceding annotation block and
        # is kept — even if a publisher marker sits later in the same block, the
        # section *start* is authoritative.
        if _STATUTORY_START_RE.match(first_line):
            in_annotation = False
            segments.append(LawSegment(text=stripped, kind="statutory"))
            continue

        heading = _looks_like_heading(first_line)
        if heading is not None and _is_annotation_heading(heading):
            in_annotation = True
            segments.append(
                LawSegment(text=stripped, kind="annotated", reason=f"heading:{heading}")
            )
            continue

        marker = _has_publisher_marker(stripped)
        if marker:
            segments.append(
                LawSegment(text=stripped, kind="annotated", reason=f"publisher:{marker}")
            )
            continue

        if in_annotation:
            segments.append(
                LawSegment(text=stripped, kind="annotated", reason="under-annotation-heading")
            )
            continue

        segments.append(LawSegment(text=stripped, kind="statutory"))

    statutory = [s for s in segments if s.kind == "statutory"]
    dropped = [s for s in segments if s.kind == "annotated"]
    statutory_text = "\n\n".join(s.text for s in statutory).strip()
    return StatutoryExtraction(
        statutory_text=statutory_text,
        segments=tuple(segments),
        dropped_annotations=tuple(dropped),
    )
