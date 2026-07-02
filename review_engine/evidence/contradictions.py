from __future__ import annotations
import re
from collections import defaultdict
from review_engine.extraction.models import SourceChunk

INVOICE = re.compile(r"(?:invoice|inv)[\s#:.-]*([A-Z0-9-]+).*?\$\s?([\d,]+(?:\.\d{2})?)", re.I)
STATUS = re.compile(r"\b(?:status|investigation status)\s*(?:is|:)?\s*(open|closed|pending|approved|denied|terminated|active)\b", re.I)
NAMED_DATE = re.compile(r"\b(termination|investigation|approval)\s+date\s*(?:is|:)?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", re.I)


def detect_contradictions(chunks: list[SourceChunk]) -> list[dict]:
    candidates = []
    invoice_values = defaultdict(lambda: defaultdict(list))
    statuses = defaultdict(list)
    dates = defaultdict(lambda: defaultdict(list))
    for chunk in chunks:
        for match in INVOICE.finditer(chunk.text):
            invoice_values[match.group(1).upper()][match.group(2).replace(",", "")].append(chunk)
        for match in STATUS.finditer(chunk.text): statuses[match.group(1).lower()].append(chunk)
        for match in NAMED_DATE.finditer(chunk.text): dates[match.group(1).lower()][match.group(2)].append(chunk)
    for invoice, amounts in invoice_values.items():
        if len(amounts) > 1:
            candidates.append(_contradiction("Conflicting invoice amounts", f"Invoice {invoice} is associated with multiple amounts: {', '.join('$' + v for v in amounts)}.", [s for group in amounts.values() for s in group]))
    if len(statuses) > 1:
        candidates.append(_contradiction("Conflicting status labels", f"Different status labels were identified: {', '.join(sorted(statuses))}. Confirm whether they refer to the same matter or event.", [s for group in statuses.values() for s in group]))
    for event, values in dates.items():
        if len(values) > 1:
            candidates.append(_contradiction(f"Conflicting {event} dates", f"Multiple {event} dates were identified: {', '.join(values)}.", [s for group in values.values() for s in group]))
    return candidates


def _contradiction(title, explanation, sources):
    return {"title": title, "category": "Contradiction", "explanation": explanation + " Requires human review.", "sources": sources[:8], "confidence": "Medium", "confidence_reason": "Conflicting structured values were extracted; entity/event identity may need confirmation.", "human_review_required": True}
