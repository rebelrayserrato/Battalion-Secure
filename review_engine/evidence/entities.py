from __future__ import annotations

import re

from review_engine.extraction.models import SourceChunk

PATTERNS = {
    "date": re.compile(
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+\d{1,2},?\s+\d{4}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
        re.I,
    ),
    "dollar_amount": re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?"),
    "company": re.compile(
        r"\b[A-Z][A-Za-z&' -]{1,50}\s(?:LLC|Inc\.?|Corp\.?|Corporation|Company|Co\.|Ltd\.?)\b"
    ),
    "person": re.compile(r"\b(?:Mr\.|Ms\.|Mrs\.|Dr\.)?\s?[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}\b"),
    "location": re.compile(
        r"\b(?:at|in|from)\s+([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})\b"
    ),
    "document_type": re.compile(
        r"\b(?:invoice|timecard|payroll|witness statement|investigation report|"
        r"termination letter|policy|expense report|approval|contract)\b",
        re.I,
    ),
}


def extract_entities(chunks: list[SourceChunk]) -> list[dict]:
    entities: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for chunk in chunks:
        for entity_type, pattern in PATTERNS.items():
            for match in pattern.finditer(chunk.text):
                value = (match.group(1) if match.lastindex else match.group(0)).strip()
                key = (entity_type, value.lower(), chunk.source_ref)
                if key not in seen:
                    entities.append(
                        {
                            "entity_type": entity_type,
                            "value": value,
                            "source_ref": chunk.source_ref,
                        }
                    )
                    seen.add(key)
        sentences = re.split(r"(?<=[.!?])\s+", chunk.text)
        for sentence in sentences:
            if PATTERNS["date"].search(sentence) and len(sentence) <= 500:
                key = ("event", sentence.lower(), chunk.source_ref)
                if key not in seen:
                    entities.append(
                        {"entity_type": "event", "value": sentence.strip(), "source_ref": chunk.source_ref}
                    )
                    seen.add(key)
    return entities
