from __future__ import annotations

from review_engine.extraction.models import SourceChunk

RULES = [
    ("Termination risk", ("terminated", "termination", "fired"), "HR Legal Risk",
     "Termination-related language requires review of documentation, consistency, and process."),
    ("Protected leave red flag", ("fmla", "medical leave", "parental leave", "protected leave"), "HR Legal Risk",
     "Protected-leave language appears near the employment events and requires jurisdiction-specific review."),
    ("Accommodation red flag", ("accommodation", "disability", "interactive process"), "HR Legal Risk",
     "Accommodation-related language requires review of the interactive process and supporting records."),
    ("Discrimination red flag", ("discrimination", "race", "religion", "gender", "age discrimination"), "HR Legal Risk",
     "Potential discrimination-related language appears in the evidence and requires human review."),
    ("Retaliation red flag", ("retaliation", "retaliated", "after reporting"), "HR Legal Risk",
     "Potential retaliation-related language appears in the evidence and requires timeline review."),
    ("Harassment red flag", ("harassment", "harassed", "hostile work environment"), "HR Legal Risk",
     "Harassment-related language appears and requires review of reports and response steps."),
    ("Final pay checklist", ("final pay", "final paycheck", "last paycheck"), "HR Legal Risk",
     "Final-pay language appears; timing and required payments depend on jurisdiction."),
]


def _matching_sources(chunks: list[SourceChunk], terms: tuple[str, ...]) -> list[SourceChunk]:
    return [chunk for chunk in chunks if any(term in chunk.text.lower() for term in terms)]


def run_hr_legal_review(chunks: list[SourceChunk], jurisdiction: str = "") -> list[dict]:
    candidates = []
    for title, terms, category, explanation in RULES:
        matches = _matching_sources(chunks, terms)
        if matches:
            candidates.append(
                {
                    "title": title,
                    "category": category,
                    "explanation": explanation + (
                        "" if jurisdiction else " Jurisdiction required."
                    ),
                    "sources": matches[:5],
                    "confidence": "Medium",
                    "confidence_reason": "Rule terms are directly present; legal significance is not determined.",
                    "human_review_required": True,
                }
            )

    all_text = " ".join(chunk.text.lower() for chunk in chunks)
    termination_sources = _matching_sources(chunks, ("terminated", "termination", "fired"))
    investigation_sources = _matching_sources(chunks, ("investigation", "complaint"))
    if investigation_sources and not any(
        term in all_text for term in ("investigation findings", "investigation report", "interview notes")
    ):
        candidates.append(
            {
                "title": "Investigation gap",
                "category": "HR Legal Risk",
                "explanation": "An investigation or complaint is referenced, but no findings, report, or interview notes were identified.",
                "sources": investigation_sources[:5],
                "confidence": "Medium",
                "confidence_reason": "The investigation reference is sourced; document-set completeness requires confirmation.",
                "human_review_required": True,
            }
        )
    trigger_sources = termination_sources or investigation_sources
    if trigger_sources:
        if "witness statement" not in all_text and "witness interview" not in all_text:
            candidates.append(
                {
                    "title": "Missing witness statements",
                    "category": "Missing Document",
                    "explanation": "A termination or investigation is referenced, but no witness statement or witness interview was identified.",
                    "sources": trigger_sources[:3],
                    "confidence": "Medium",
                    "confidence_reason": "The triggering event is sourced; absence is based only on processed documents.",
                    "human_review_required": True,
                }
            )
    if termination_sources:
        if not any(term in all_text for term in ("policy", "handbook", "procedure")):
            candidates.append(
                {
                    "title": "Missing policy reference",
                    "category": "Missing Document",
                    "explanation": "Termination evidence is present, but no policy, handbook, or procedure reference was identified.",
                    "sources": termination_sources[:3],
                    "confidence": "Medium",
                    "confidence_reason": "The triggering event is sourced; document-set completeness requires confirmation.",
                    "human_review_required": True,
                }
            )
        candidates.append(
            {
                "title": "Attorney review required",
                "category": "HR Legal Risk",
                "explanation": "A termination-related event was identified. This is a review flag, not a legal conclusion.",
                "sources": termination_sources[:5],
                "confidence": "High",
                "confidence_reason": "Termination language is directly present in cited sources.",
                "human_review_required": True,
            }
        )
    return candidates
