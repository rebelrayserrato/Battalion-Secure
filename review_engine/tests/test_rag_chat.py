"""Tests for RAYAAAA-232 grounded RAG chat.

Covers the two things the issue calls out explicitly: retrieval grounding (answers
and citations come only from the local index) and the no-external-facts guardrail
(the model is never given, and never invents, material outside the retrieved
chunks; it is not even called when nothing is retrieved).
"""
from review_engine.app.rag_chat import (
    MODEL_UNAVAILABLE_NOTICE,
    NO_EVIDENCE_MESSAGE,
    RagChatService,
)
from review_engine.llm_connectors.ollama import GROUNDED_NO_CONTEXT, build_grounded_prompt


# --- helpers ----------------------------------------------------------------


def _row(source_ref, text, citation=None, distance=0.1):
    return {
        "source_ref": source_ref,
        "text": text,
        "citation": citation or f"doc.txt ({source_ref})",
        "distance": distance,
    }


class FakeConnector:
    """Records calls so tests can prove the model only ever sees retrieved chunks."""

    def __init__(self, is_available=True, reply="DRAFTED: see [SRC-A]."):
        self._available = is_available
        self._reply = reply
        self.calls = []

    def available(self):
        return self._available

    def answer_from_context(self, question, contexts):
        self.calls.append({"question": question, "contexts": contexts})
        return self._reply


# --- grounding --------------------------------------------------------------


def test_answer_cites_only_retrieved_source_refs():
    rows = [_row("SRC-A", "Termination effective 2025-03-01."), _row("SRC-B", "Invoice 1042 approved by J. Doe.")]
    fake = FakeConnector()
    service = RagChatService(retriever=lambda q, k: rows, connector=fake)

    result = service.answer("When was termination effective?")

    assert result.grounded is True
    assert result.model_used is True
    # Citations are exactly the retrieved chunks — nothing more, nothing less.
    assert result.source_refs == ["SRC-A", "SRC-B"]
    # The model was handed only the retrieved passages.
    passed_refs = [c["source_ref"] for c in fake.calls[0]["contexts"]]
    assert passed_refs == ["SRC-A", "SRC-B"]


def test_retriever_limit_is_forwarded():
    seen = {}

    def retriever(question, limit):
        seen["limit"] = limit
        return [_row("SRC-A", "text")]

    RagChatService(retriever=retriever, connector=FakeConnector()).answer("q", limit=3)
    assert seen["limit"] == 3


# --- no-external-facts guardrail --------------------------------------------


def test_no_retrieval_never_invokes_model():
    fake = FakeConnector(reply="FABRICATED OUTSIDE FACT")
    service = RagChatService(retriever=lambda q, k: [], connector=fake)

    result = service.answer("What is the capital of France?")

    # Nothing retrieved -> nothing to ground on -> model must not run at all.
    assert fake.calls == []
    assert result.grounded is False
    assert result.model_used is False
    assert result.text == NO_EVIDENCE_MESSAGE
    assert result.sources == []


def test_grounded_prompt_contains_only_supplied_passages():
    contexts = [
        {"source_ref": "SRC-A", "text": "The lease term is 12 months.", "citation": "l.txt (SRC-A)"},
    ]
    prompt = build_grounded_prompt("How long is the lease?", contexts)

    assert "SRC-A" in prompt
    assert "The lease term is 12 months." in prompt
    # Guardrail language is present…
    assert "ONLY facts stated in the passages" in prompt
    assert "requires human review" in prompt.lower()
    # …and no unrelated/outside content leaks in.
    assert "SRC-B" not in prompt
    assert "capital of France" not in prompt


def test_empty_context_yields_human_review_reply():
    from review_engine.llm_connectors.ollama import OllamaConnector

    assert OllamaConnector().answer_from_context("q", []) == GROUNDED_NO_CONTEXT
    assert "human review" in GROUNDED_NO_CONTEXT.lower()


# --- graceful degradation ---------------------------------------------------


def test_falls_back_to_verbatim_excerpts_when_ollama_unavailable():
    rows = [_row("SRC-A", "Termination effective 2025-03-01.")]
    fake = FakeConnector(is_available=False, reply="SHOULD NOT RUN")
    service = RagChatService(retriever=lambda q, k: rows, connector=fake)

    result = service.answer("When?")

    assert fake.calls == []  # unavailable model is not called
    assert result.model_used is False
    assert result.grounded is True
    assert result.notice == MODEL_UNAVAILABLE_NOTICE
    # The fallback echoes retrieved text + citation only (no generated prose).
    assert "Termination effective 2025-03-01." in result.text
    assert "SRC-A" in result.text


def test_no_connector_configured_degrades_to_excerpts():
    rows = [_row("SRC-A", "Signed by both parties.")]
    result = RagChatService(retriever=lambda q, k: rows, connector=None).answer("q")
    assert result.model_used is False
    assert result.grounded is True
    assert "Signed by both parties." in result.text


def test_model_error_falls_back_instead_of_crashing():
    class Boom(FakeConnector):
        def answer_from_context(self, question, contexts):
            raise RuntimeError("connection refused")

    rows = [_row("SRC-A", "Grounded text.")]
    result = RagChatService(retriever=lambda q, k: rows, connector=Boom()).answer("q")
    assert result.model_used is False
    assert result.grounded is True
    assert "Grounded text." in result.text


def test_blank_question_is_a_no_op():
    called = {"n": 0}

    def retriever(q, k):
        called["n"] += 1
        return []

    result = RagChatService(retriever=retriever, connector=None).answer("   ")
    assert called["n"] == 0
    assert result.grounded is False


# --- integration with the real local index ----------------------------------


def test_real_index_grounds_and_excludes_unindexed_facts(tmp_path):
    from review_engine.evidence.index import EvidenceIndex
    from review_engine.extraction.models import SourceChunk, source_reference

    matter_id = "M-RAG"
    chunks = [
        SourceChunk(
            matter_id, "contract.txt", "txt",
            "The employment contract was terminated on 01 March 2025 by mutual agreement.",
            source_reference(matter_id, "contract.txt", section="body", ordinal=0),
            section="body",
        ),
        SourceChunk(
            matter_id, "invoice.txt", "txt",
            "Invoice 1042 for consulting services was approved by the finance director.",
            source_reference(matter_id, "invoice.txt", section="body", ordinal=1),
            section="body",
        ),
    ]
    index = EvidenceIndex(matter_id, root=tmp_path)
    assert index.build(chunks) == 2

    # No connector -> verbatim excerpts, but still fully grounded in the index.
    service = RagChatService(retriever=index.search, connector=None)
    result = service.answer("When was the contract terminated?", limit=5)

    assert result.grounded is True
    indexed_refs = {c.source_ref for c in chunks}
    # Every cited ref exists in the index — no citation is invented.
    assert set(result.source_refs).issubset(indexed_refs)
    # The termination chunk is the top hit for a termination question.
    assert result.sources[0].source_ref == chunks[0].source_ref
    # A fact that is not in any indexed chunk never appears in the answer.
    assert "capital of france" not in result.text.lower()
