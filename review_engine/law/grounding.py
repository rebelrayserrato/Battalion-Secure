"""Law-grounded composed retrieval + answering (RAYAAAA-251, Phase C).

Extends the RAYAAAA-245 client-scoped composition with a THIRD, logically
distinct corpus: the jurisdiction-filtered law reference library. A law-grounded
question for a Task pulls, and keeps distinct by an ``origin`` tag:

* ``task``   — the Task's own document index,
* ``policy`` — the linked Client's policy library (client-scoped),
* ``law``    — the law corpus for ``{client state} ∪ {federal}`` ONLY.

The jurisdiction hard-filter (AC C) is enforced structurally: the retriever opens
only the law partitions returned by :func:`resolve_law_jurisdictions`, so a Task
whose client is in State A never instantiates State B's law store.

The answerer renders the counsel-bound disclaimer (AC G) on every answer, stamps
each law citation with its provenance (AC F), and runs the
no-citation-without-retrieved-chunk guardrail (AC E) over any drafted text.
Local only; degrades to verbatim retrieved passages when Ollama is unavailable —
it never fabricates law text or a citation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from review_engine.evidence.index import EvidenceIndex
from review_engine.law.library import (
    LAW_DISCLAIMER,
    LawLibraryIndex,
    citation_stamp,
    enforce_law_citation_guardrail,
    resolve_law_jurisdictions,
)
from review_engine.llm_connectors.ollama import OllamaConnector

# Retriever contract matches the RAYAAAA-245 one: (matter_id, query, limit) -> rows.
Retriever = Callable[[str, str, int], list[dict]]

LAW_GROUNDING_RULES = (
    "You are an evidence-bound review assistant. Use ONLY the numbered CONTEXT "
    "passages below. Do not add facts and do not state what the law means or "
    "give legal advice. When you refer to a statute or regulation section, cite "
    "ONLY a section that appears verbatim in a LAW passage below and quote it; "
    "never state a section number from memory. If the context does not contain "
    "the answer or the relevant section, say 'not in reference library'. Cite "
    "the source-reference ID (SRC-XXXX) of every passage you rely on. End with "
    "'Requires human review.'"
)


def _tag(rows: list[dict], origin: str) -> list[dict]:
    for row in rows:
        row.setdefault("origin", origin)
    return rows


def compose_law_grounded_rows(
    task_rows: list[dict],
    policy_rows: list[dict],
    law_rows: list[dict],
    limit: int,
) -> list[dict]:
    """Merge the three distinct corpora, nearest-first, keeping the origin tag."""
    merged = (
        _tag(list(task_rows), "task")
        + _tag(list(policy_rows), "policy")
        + _tag(list(law_rows), "law")
    )
    merged.sort(key=lambda r: r.get("distance", 0.0))
    return merged[:limit]


def make_law_grounded_retriever(
    db,
    *,
    task_index_factory: Optional[Callable] = None,
    policy_index_factory: Optional[Callable] = None,
    law_index_factory: Optional[Callable] = None,
) -> Retriever:
    """Build a ``(matter_id, query, limit)`` retriever composing Task docs +
    linked-client policy library + jurisdiction-filtered law corpus.

    Index factories are injectable so tests can prove the jurisdiction isolation
    boundary without chromadb; they default to the real on-disk indexes.
    """
    from review_engine.clients.policy_library import PolicyLibraryIndex

    make_task_index = task_index_factory or EvidenceIndex
    make_policy_index = policy_index_factory or PolicyLibraryIndex
    make_law_index = law_index_factory or LawLibraryIndex

    def retriever(matter_id: str, query: str, limit: int) -> list[dict]:
        task_rows = make_task_index(matter_id).search(query, limit) or []
        matter = db.get_matter(matter_id) or {}
        client_id = matter.get("client_id")
        policy_rows: list[dict] = []
        if client_id:
            policy_rows = make_policy_index(client_id).search(query, limit) or []
        # AC C hard filter: open ONLY the resolved jurisdiction partitions.
        law_rows: list[dict] = []
        for jurisdiction in resolve_law_jurisdictions(matter.get("jurisdiction")):
            law_rows.extend(make_law_index(jurisdiction).search(query, limit) or [])
        return compose_law_grounded_rows(
            list(task_rows), list(policy_rows), list(law_rows), limit
        )

    return retriever


def render_law_source(row: dict) -> str:
    """Verbatim quote + pinpoint cite + provenance stamp for one law row (AC F)."""
    stamp = citation_stamp(
        row.get("law_source_name", ""),
        row.get("law_retrieved", ""),
        row.get("law_effective", ""),
    )
    quote = (row.get("text", "") or "").strip()
    citation = row.get("citation", row.get("source_ref", ""))
    return f'> "{quote}"\n{citation} {stamp}'


def build_context_block(rows: list[dict]) -> str:
    """Numbered, corpus-tagged passages. Law passages carry their provenance so
    the model can only reproduce a real, attributed section."""
    lines = []
    for position, row in enumerate(rows, start=1):
        origin = row.get("origin", "task").upper()
        header = f"[{position}] ({origin} · {row['source_ref']} — {row.get('citation', row['source_ref'])})"
        body = (row.get("text", "") or "").strip()
        if row.get("origin") == "law":
            body += "\n" + citation_stamp(
                row.get("law_source_name", ""),
                row.get("law_retrieved", ""),
                row.get("law_effective", ""),
            )
        lines.append(f"{header}\n{body}")
    return "\n\n".join(lines)


@dataclass
class LawGroundedAnswer:
    answer: str
    disclaimer: str = LAW_DISCLAIMER
    task_sources: list = field(default_factory=list)
    policy_sources: list = field(default_factory=list)
    law_sources: list = field(default_factory=list)
    grounded: bool = False
    model_used: bool = False
    redacted_citations: list = field(default_factory=list)
    human_review_required: bool = True


class LawGroundedAnswerer:
    """RAG answerer that composes Task + client-policy + jurisdiction law corpora,
    stamps law citations, redacts unbacked citations, and always renders the
    counsel-bound disclaimer."""

    def __init__(
        self,
        retriever: Retriever,
        connector: Optional[OllamaConnector] = None,
    ):
        self.retriever = retriever
        self.connector = connector or OllamaConnector()

    def answer(self, matter_id: str, question: str, limit: int = 8) -> LawGroundedAnswer:
        rows = self.retriever(matter_id, question, limit) or []
        law_rows = [r for r in rows if r.get("origin") == "law"]
        task_sources = [_source_view(r) for r in rows if r.get("origin") == "task"]
        policy_sources = [_source_view(r) for r in rows if r.get("origin") == "policy"]
        law_sources = [_law_source_view(r) for r in law_rows]

        if not rows:
            return LawGroundedAnswer(
                answer=(
                    "No indexed evidence — in this Task's documents, the client's "
                    "policy library, or the jurisdiction's law reference library — "
                    "matched this question. Requires human review."
                ),
                grounded=False,
                model_used=False,
            )

        # Ollama unavailable -> never fabricate. Hand back verbatim passages; law
        # passages are rendered as quote + pinpoint cite + provenance stamp.
        if not self.connector.available():
            passages = []
            for row in rows:
                if row.get("origin") == "law":
                    passages.append(render_law_source(row))
                else:
                    passages.append(
                        f"- {row.get('citation', row['source_ref'])}: "
                        f"{(row.get('text', '') or '').strip()}"
                    )
            body = (
                "Local model unavailable — showing the most relevant retrieved "
                "passages instead of a drafted answer:\n\n" + "\n\n".join(passages)
            )
            # Guardrail still applies to whatever text we surface.
            body, redacted = enforce_law_citation_guardrail(body, law_rows)
            return LawGroundedAnswer(
                answer=body,
                task_sources=task_sources,
                policy_sources=policy_sources,
                law_sources=law_sources,
                grounded=True,
                model_used=False,
                redacted_citations=redacted,
            )

        prompt = (
            f"{LAW_GROUNDING_RULES}\n\nQUESTION: {question}\n\n"
            f"CONTEXT:\n{build_context_block(rows)}\n\nANSWER:"
        )
        try:
            drafted = self.connector.generate(prompt) or ""
        except Exception:
            drafted = ""
        # AC E: sanitize unconditionally before the text can be returned.
        sanitized, redacted = enforce_law_citation_guardrail(drafted, law_rows)
        return LawGroundedAnswer(
            answer=sanitized,
            task_sources=task_sources,
            policy_sources=policy_sources,
            law_sources=law_sources,
            grounded=True,
            model_used=bool(drafted),
            redacted_citations=redacted,
        )


def _source_view(row: dict) -> dict:
    return {
        "source_ref": row["source_ref"],
        "citation": row.get("citation", row["source_ref"]),
    }


def _law_source_view(row: dict) -> dict:
    return {
        "source_ref": row["source_ref"],
        "citation": row.get("citation", row["source_ref"]),
        "quote": (row.get("text", "") or "").strip(),
        "stamp": citation_stamp(
            row.get("law_source_name", ""),
            row.get("law_retrieved", ""),
            row.get("law_effective", ""),
        ),
        "source_url": row.get("law_source_url", ""),
    }
