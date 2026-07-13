"""Grounded retrieval-augmented chat over a Task's documents (RAYAAAA-232).

Phase 2a of the RAYAAAA-229 document-intelligence roadmap. This orchestrates the
existing local pieces only:

* retrieval  -> ``evidence.index.EvidenceIndex`` (local Chroma + local embeddings)
* generation -> ``llm_connectors.ollama.OllamaConnector`` (bounded, local Ollama)

Guardrails match the existing summarizer: answers are drawn ONLY from retrieved
chunks, no new facts or legal conclusions are added, and the flow degrades
gracefully — if nothing is retrieved the model is never called, and if Ollama is
unavailable the user still gets the verbatim source excerpts with citations.

Everything stays local (no external API / no egress) and operates on SYNTHETIC /
owner-internal data only; this phase does not touch real client PII (that gate is
Phase 4 of RAYAAAA-229).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from review_engine.llm_connectors.ollama import GROUNDED_NO_CONTEXT, OllamaConnector

# How many chunks to retrieve for an answer by default.
DEFAULT_TOP_K = 6

NO_EVIDENCE_MESSAGE = (
    "No indexed evidence in this Task's documents matches that question. "
    "Upload and process documents first, or escalate for human review."
)
MODEL_UNAVAILABLE_NOTICE = (
    "Local drafting model (Ollama) is unavailable — showing the most relevant "
    "source excerpts verbatim. Requires human review."
)

# A retriever takes (question, limit) and returns the raw search rows produced by
# EvidenceIndex.search: dicts with source_ref / text / citation / distance.
Retriever = Callable[[str, int], list[dict]]


@dataclass(frozen=True)
class RetrievedSource:
    source_ref: str
    citation: str
    text: str
    distance: float


@dataclass(frozen=True)
class RagAnswer:
    text: str
    sources: list[RetrievedSource] = field(default_factory=list)
    # grounded: at least one source chunk backed this answer.
    grounded: bool = False
    # model_used: the answer was drafted by Ollama (vs. the extractive fallback).
    model_used: bool = False
    notice: Optional[str] = None

    @property
    def source_refs(self) -> list[str]:
        return [source.source_ref for source in self.sources]


def _extractive_answer(sources: Sequence[RetrievedSource]) -> str:
    """Fallback 'answer' used when Ollama is unavailable.

    This never generates prose — it only echoes the retrieved passages with their
    source-reference IDs, so it cannot introduce a fact that is not in the index.
    """
    lines = []
    for source in sources:
        lines.append(f"- {source.text.strip()} [{source.source_ref}]")
    return "\n".join(lines)


class RagChatService:
    """Answer questions about a Task strictly from its local evidence index."""

    def __init__(
        self,
        retriever: Retriever,
        connector: Optional[OllamaConnector] = None,
    ):
        self._retriever = retriever
        self._connector = connector

    @classmethod
    def for_matter(
        cls,
        matter_id: str,
        connector: Optional[OllamaConnector] = None,
        db=None,
    ) -> "RagChatService":
        """Wire the service to a matter's on-disk Chroma index.

        Imported lazily so unit tests can drive the service with a fake retriever
        without pulling in Chroma / the extraction dependency chain.

        When ``db`` is supplied (RAYAAAA-245), retrieval composes the Task's own
        index with ONLY its linked Client's policy library — never another
        client's — so chat answers can cite the client's actual policies.
        """
        from review_engine.evidence.index import EvidenceIndex

        if db is not None:
            from review_engine.app.retrieval import make_client_scoped_retriever

            scoped = make_client_scoped_retriever(db)
            # Adapt the (matter_id, query, limit) composed retriever to the
            # chat service's (question, limit) shape by binding this matter.
            def retriever(question: str, limit: int) -> list[dict]:
                return scoped(matter_id, question, limit)

            return cls(retriever=retriever, connector=connector)

        index = EvidenceIndex(matter_id)
        return cls(retriever=index.search, connector=connector)

    def answer(self, question: str, limit: int = DEFAULT_TOP_K) -> RagAnswer:
        question = (question or "").strip()
        if not question:
            return RagAnswer(
                text="Ask a question about this Task's documents.",
                grounded=False,
            )

        rows = self._retriever(question, limit) or []
        sources = [
            RetrievedSource(
                source_ref=row["source_ref"],
                citation=row["citation"],
                text=row["text"],
                distance=float(row.get("distance", 0.0)),
            )
            for row in rows
        ]

        # No evidence retrieved -> never invoke the model. There is nothing to
        # ground an answer in, so we cannot (and must not) generate one.
        if not sources:
            return RagAnswer(
                text=NO_EVIDENCE_MESSAGE,
                sources=[],
                grounded=False,
                model_used=False,
            )

        # Ollama unavailable (or not configured) -> degrade to verbatim excerpts.
        if self._connector is None or not self._connector.available():
            return RagAnswer(
                text=_extractive_answer(sources),
                sources=sources,
                grounded=True,
                model_used=False,
                notice=MODEL_UNAVAILABLE_NOTICE,
            )

        contexts = [
            {"source_ref": source.source_ref, "text": source.text, "citation": source.citation}
            for source in sources
        ]
        try:
            drafted = self._connector.answer_from_context(question, contexts)
        except Exception:
            # Any transport / model error falls back to the grounded excerpts
            # rather than failing the chat outright.
            return RagAnswer(
                text=_extractive_answer(sources),
                sources=sources,
                grounded=True,
                model_used=False,
                notice=MODEL_UNAVAILABLE_NOTICE,
            )
        return RagAnswer(
            text=drafted or GROUNDED_NO_CONTEXT,
            sources=sources,
            grounded=True,
            model_used=True,
        )
