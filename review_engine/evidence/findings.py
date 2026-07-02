from __future__ import annotations

from review_engine.extraction.models import SourceChunk

VALID_CATEGORIES = {
    "HR Legal Risk",
    "Fraud Red Flag",
    "Missing Document",
    "Contradiction",
    "Timeline Issue",
    "Unsupported Finding",
}
VALID_CONFIDENCE = {"Low", "Medium", "High"}


def create_finding(candidate: dict) -> dict | None:
    sources: list[SourceChunk] = candidate.get("sources") or []
    if not sources:
        return None
    category = candidate.get("category", "Unsupported Finding")
    if category not in VALID_CATEGORIES:
        category = "Unsupported Finding"
    confidence = candidate.get("confidence", "Low")
    if confidence not in VALID_CONFIDENCE:
        confidence = "Low"
    unique = {}
    for source in sources:
        unique[source.source_ref] = {
            "source_ref": source.source_ref,
            "document_name": source.document_name,
            "page": source.page,
            "row": source.row,
            "section": source.section,
            "citation": source.citation,
        }
    return {
        "title": candidate["title"],
        "category": category,
        "explanation": candidate["explanation"],
        "supporting_sources": list(unique.values()),
        "confidence": confidence,
        "confidence_reason": candidate.get("confidence_reason", "Rule-based match requires human review."),
        "human_review_required": bool(candidate.get("human_review_required", True)),
    }


def finalize_findings(candidates: list[dict]) -> list[dict]:
    findings = []
    seen = set()
    for candidate in candidates:
        finding = create_finding(candidate)
        if finding:
            key = (
                finding["title"],
                tuple(source["source_ref"] for source in finding["supporting_sources"]),
            )
            if key not in seen:
                findings.append(finding)
                seen.add(key)
    return findings
