from __future__ import annotations

import re
from datetime import datetime

from dateutil import parser as date_parser

from review_engine.evidence.entities import PATTERNS
from review_engine.extraction.models import SourceChunk


def _parse_date(value: str) -> datetime | None:
    try:
        return date_parser.parse(value, fuzzy=False, dayfirst=False)
    except (ValueError, OverflowError):
        return None


def build_timeline(chunks: list[SourceChunk]) -> list[dict]:
    timeline = []
    seen: set[tuple[str, str, str]] = set()
    for chunk in chunks:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", chunk.text):
            for match in PATTERNS["date"].finditer(sentence):
                parsed = _parse_date(match.group(0))
                if not parsed:
                    continue
                event = sentence.strip()[:500]
                key = (parsed.date().isoformat(), event, chunk.source_ref)
                if key not in seen:
                    timeline.append(
                        {
                            "date": parsed.date().isoformat(),
                            "event": event,
                            "source_ref": chunk.source_ref,
                            "citation": chunk.citation,
                        }
                    )
                    seen.add(key)
    return sorted(timeline, key=lambda item: (item["date"], item["source_ref"]))
