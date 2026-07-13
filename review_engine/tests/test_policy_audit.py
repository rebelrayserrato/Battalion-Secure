"""Tests for the policy-audit / 'before you sign' templated-review path (P2b).

No chromadb / no live Ollama needed: retrieval and the local model are injected
as fakes so we can assert the grounding guardrails deterministically.
"""
from __future__ import annotations

import json

from review_engine.app.policy_audit import (
    FLAG_CATEGORY,
    MISSING_CATEGORY,
    REVIEW_CATEGORY,
    PolicyAuditor,
)
from review_engine.app.retrieval import GroundedAnswerer

CHECKLIST = [
    {"id": "liability", "label": "Liability & indemnity", "query": "liability cap indemnity"},
    {"id": "termination", "label": "Termination & notice", "query": "termination notice"},
]

ROWS = {
    "liability cap indemnity": [
        {"source_ref": "SRC-AAAA1111", "text": "The Supplier accepts unlimited liability for all losses.",
         "citation": "msa.pdf, page 4 (SRC-AAAA1111)", "document_name": "msa.pdf", "page": 4, "row": -1, "section": ""},
    ],
    "termination notice": [
        {"source_ref": "SRC-BBBB2222", "text": "Either party may terminate on 30 days notice.",
         "citation": "msa.pdf, page 2 (SRC-BBBB2222)", "document_name": "msa.pdf", "page": 2, "row": -1, "section": ""},
    ],
}


def fake_retriever(matter_id, query, limit):
    return ROWS.get(query, [])


class FakeConnector:
    """Injectable stand-in for OllamaConnector."""

    def __init__(self, available=True, replies=None, default_reply="{}"):
        self._available = available
        self._replies = replies or {}
        self._default = default_reply
        self.prompts = []

    def available(self):
        return self._available

    def generate(self, prompt, timeout=120):
        self.prompts.append(prompt)
        for needle, reply in self._replies.items():
            if needle in prompt:
                return reply
        return self._default


def _refs(finding):
    return {s["source_ref"] for s in finding["supporting_sources"]}


def test_flag_finding_cites_only_retrieved_source_refs():
    reply = json.dumps({"status": "flag", "explanation": "Unlimited liability is one-sided (SRC-AAAA1111).",
                        "source_refs": ["SRC-AAAA1111"], "confidence": "High"})
    connector = FakeConnector(replies={"Liability & indemnity": reply}, default_reply=json.dumps({"status": "ok"}))
    findings = PolicyAuditor(connector=connector, retriever=fake_retriever).audit("m1", CHECKLIST)

    flags = [f for f in findings if f["category"] == FLAG_CATEGORY]
    assert len(flags) == 1
    assert _refs(flags[0]) == {"SRC-AAAA1111"}
    assert flags[0]["human_review_required"] is True
    assert "human review" in flags[0]["explanation"].lower()


def test_missing_status_becomes_missing_protection():
    reply = json.dumps({"status": "missing", "explanation": "No cap on liability found.",
                        "source_refs": ["SRC-AAAA1111"], "confidence": "Medium"})
    connector = FakeConnector(replies={"Liability & indemnity": reply}, default_reply=json.dumps({"status": "ok"}))
    findings = PolicyAuditor(connector=connector, retriever=fake_retriever).audit("m1", CHECKLIST)
    missing = [f for f in findings if f["category"] == MISSING_CATEGORY]
    assert len(missing) == 1
    assert _refs(missing[0]) == {"SRC-AAAA1111"}


def test_ok_status_produces_no_finding():
    connector = FakeConnector(default_reply=json.dumps({"status": "ok"}))
    findings = PolicyAuditor(connector=connector, retriever=fake_retriever).audit("m1", CHECKLIST)
    assert findings == []


def test_hallucinated_source_ref_is_dropped_no_external_facts():
    # Model cites a ref that was never retrieved — it must not leak into sources.
    reply = json.dumps({"status": "flag", "explanation": "Risk per SRC-DEADBEEF.",
                        "source_refs": ["SRC-DEADBEEF"], "confidence": "High"})
    connector = FakeConnector(replies={"Liability & indemnity": reply}, default_reply=json.dumps({"status": "ok"}))
    findings = PolicyAuditor(connector=connector, retriever=fake_retriever).audit("m1", CHECKLIST)
    flags = [f for f in findings if f["category"] == FLAG_CATEGORY]
    assert len(flags) == 1
    # hallucinated ref dropped; falls back to the actually-retrieved passage
    assert "SRC-DEADBEEF" not in _refs(flags[0])
    assert _refs(flags[0]) == {"SRC-AAAA1111"}


def test_unparseable_model_output_does_not_fabricate():
    connector = FakeConnector(default_reply="I think this contract looks fine, trust me!")
    findings = PolicyAuditor(connector=connector, retriever=fake_retriever).audit("m1", CHECKLIST)
    # No JSON -> no verdict invented; every finding is a review-required fallback
    assert findings
    assert all(f["category"] == REVIEW_CATEGORY for f in findings)
    assert all(f["human_review_required"] for f in findings)


def test_degrade_path_when_model_unavailable():
    connector = FakeConnector(available=False)
    checklist = CHECKLIST + [{"id": "x", "label": "Insurance", "query": "no-such-evidence"}]
    findings = PolicyAuditor(connector=connector, retriever=fake_retriever).audit("m1", checklist)
    # Items with retrieved evidence -> review-required with the retrieved refs
    review = [f for f in findings if f["category"] == REVIEW_CATEGORY]
    assert review and all(_refs(f) for f in review)
    # Item with no matching evidence -> flagged as a potentially missing protection
    missing = [f for f in findings if f["category"] == MISSING_CATEGORY]
    assert any("Insurance" in f["title"] for f in missing)


def test_grounded_answerer_degrades_without_external_facts():
    connector = FakeConnector(available=False)
    answerer = GroundedAnswerer(connector=connector, retriever=fake_retriever)
    result = answerer.answer("m1", "liability cap indemnity", limit=4)
    assert result["model_used"] is False
    assert result["grounded"] is True
    assert result["human_review_required"] is True
    # Only retrieved evidence is echoed back; the retrieved ref is cited.
    assert {s["source_ref"] for s in result["sources"]} == {"SRC-AAAA1111"}
    assert "unlimited liability" in result["answer"].lower()


def test_grounded_answerer_uses_only_context_when_model_available():
    connector = FakeConnector(replies={"CONTEXT:": "30 days notice applies (SRC-BBBB2222). Requires human review."})
    answerer = GroundedAnswerer(connector=connector, retriever=fake_retriever)
    result = answerer.answer("m1", "termination notice")
    assert result["model_used"] is True
    assert {s["source_ref"] for s in result["sources"]} == {"SRC-BBBB2222"}
    # The grounding rules were sent to the model.
    assert "ONLY the numbered CONTEXT" in connector.prompts[0]
