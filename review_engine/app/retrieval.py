"""Grounded retrieval-augmented answering over a Task's local evidence index.

Shared backend for the RAG "Chat" mode (RAYAAAA-232 / P2a) and the policy-audit
"before you sign" mode (RAYAAAA-233 / P2b). Everything here is LOCAL only: it
reads the on-disk Chroma index built by ``EvidenceIndex`` and, when a local
Ollama model is reachable, drafts an answer that is bound to the retrieved
passages. No external API calls, no egress. Consistent with the current
Chroma + local sentence-transformers + local Ollama posture.

Guardrails (identical spirit to the existing summarizer):
- Answer ONLY from the retrieved passages; add no new facts or legal conclusions.
- Always surface the source-reference IDs that were used.
- Say 'requires human review' — this is a screening aid, not advice.
- Degrade gracefully when Ollama is unavailable: return the retrieved passages
  verbatim with a note, never a fabricated answer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from review_engine.evidence.index import EvidenceIndex
from review_engine.llm_connectors.ollama import OllamaConnector

HUMAN_REVIEW_NOTE = (
    "This is an automated, evidence-bound screening aid — not legal advice. "
    "Requires human review before you rely on it."
)

# Instructions injected into every grounded prompt. Kept in one place so the
# chat mode and the policy-audit mode share the exact same no-external-facts
# contract with the local model.
GROUNDING_RULES = (
    "You are an evidence-bound review assistant. Use ONLY the numbered CONTEXT "
    "passages below. Do not add facts, do not draw legal conclusions, do not "
    "state that fraud or a breach occurred. If the context does not answer the "
    "question, say so plainly. Cite the source-reference ID (e.g. SRC-XXXX) of "
    "every passage you rely on. End with 'Requires human review.'"
)

# A retriever is any callable (matter_id, query, limit) -> list[dict rows].
# Defaults to the local Chroma index; injectable so tests need no chromadb.
Retriever = Callable[[str, str, int], list[dict]]


def default_retriever(matter_id: str, query: str, limit: int) -> list[dict]:
    return EvidenceIndex(matter_id).search(query, limit)


@dataclass(frozen=True)
class RetrievedSource:
    """Adapts a retrieval row to the attribute shape ``create_finding`` expects."""

    source_ref: str
    document_name: str
    page: Optional[int]
    row: Optional[int]
    section: Optional[str]
    citation: str
    text: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "RetrievedSource":
        page = row.get("page")
        rownum = row.get("row")
        return cls(
            source_ref=row["source_ref"],
            document_name=row.get("document_name", ""),
            page=page if page not in (-1, None) else None,
            row=rownum if rownum not in (-1, None) else None,
            section=row.get("section") or None,
            citation=row.get("citation", row["source_ref"]),
            text=row.get("text", ""),
        )


def build_context_block(rows: list[dict]) -> str:
    """Render retrieved rows as numbered, source-tagged passages for a prompt."""
    lines = []
    for position, row in enumerate(rows, start=1):
        lines.append(
            f"[{position}] ({row['source_ref']} — {row.get('citation', row['source_ref'])})\n"
            f"{row.get('text', '').strip()}"
        )
    return "\n\n".join(lines)


def allowed_source_refs(rows: list[dict]) -> set[str]:
    return {row["source_ref"] for row in rows}


class GroundedAnswerer:
    """RAG answerer used by the Chat mode; retrieval + grounded local generation."""

    def __init__(
        self,
        connector: Optional[OllamaConnector] = None,
        retriever: Optional[Retriever] = None,
    ):
        self.connector = connector or OllamaConnector()
        self.retriever = retriever or default_retriever

    def answer(self, matter_id: str, question: str, limit: int = 8) -> dict:
        rows = self.retriever(matter_id, question, limit)
        sources = [
            {"source_ref": r["source_ref"], "citation": r.get("citation", r["source_ref"])}
            for r in rows
        ]
        if not rows:
            return {
                "answer": (
                    "No indexed evidence matched this question. Process the Task's "
                    "documents first, or rephrase. " + HUMAN_REVIEW_NOTE
                ),
                "sources": [],
                "grounded": False,
                "model_used": False,
                "human_review_required": True,
            }

        context = build_context_block(rows)
        if not self.connector.available():
            # Degrade: never fabricate. Hand back the retrieved passages verbatim.
            passages = "\n\n".join(
                f"- {r.get('citation', r['source_ref'])}: {r.get('text', '').strip()}"
                for r in rows
            )
            return {
                "answer": (
                    "Local model unavailable — showing the most relevant retrieved "
                    "passages instead of a drafted answer:\n\n"
                    f"{passages}\n\n{HUMAN_REVIEW_NOTE}"
                ),
                "sources": sources,
                "grounded": True,
                "model_used": False,
                "human_review_required": True,
            }

        prompt = (
            f"{GROUNDING_RULES}\n\nQUESTION: {question}\n\nCONTEXT:\n{context}\n\nANSWER:"
        )
        drafted = self.connector.generate(prompt)
        return {
            "answer": drafted,
            "sources": sources,
            "grounded": True,
            "model_used": True,
            "human_review_required": True,
        }
