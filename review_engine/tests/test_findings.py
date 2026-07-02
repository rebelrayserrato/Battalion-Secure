from review_engine.evidence.findings import create_finding, finalize_findings


def test_no_source_means_no_finding():
    candidate = {"title": "Unsupported assertion", "category": "Fraud Red Flag",
        "explanation": "No evidence.", "sources": [], "confidence": "High",
        "confidence_reason": "None", "human_review_required": True}
    assert create_finding(candidate) is None
    assert finalize_findings([candidate]) == []
