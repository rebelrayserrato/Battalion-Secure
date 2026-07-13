"""Policy-audit / 'before you sign' assistant mode (RAYAAAA-233 / P2b).

A prompt-templated review that runs over the SAME local retrieval as the RAG
chat mode (RAYAAAA-232 / P2a). For each item in a policy/checklist it retrieves
the most relevant passages from the Task's local Chroma index, asks the local
Ollama model whether the document contains that protection or any unusual/risky
terms, and turns the answer into findings that reuse the existing
findings/source-reference model — every finding cites retrieved source-ref IDs
and carries the standard 'requires human review' framing.

LOCAL only (Chroma + local sentence-transformers + local Ollama); no external
API, no egress. SYNTHETIC / owner-internal data only — this phase does not touch
real client PII (that gate is Phase 4).
"""
from __future__ import annotations

import json
import re
from typing import Optional

from review_engine.app.retrieval import (
    HUMAN_REVIEW_NOTE,
    RetrievedSource,
    Retriever,
    allowed_source_refs,
    build_context_block,
    default_retriever,
)
from review_engine.evidence.findings import (
    VALID_CATEGORIES,
    VALID_CONFIDENCE,
    finalize_findings,
)
from review_engine.llm_connectors.ollama import OllamaConnector

CONFIDENCE_REASON = (
    "Automated checklist screen against retrieved evidence; requires "
    "human-in-the-loop review before signing or relying on this document."
)

# A default 'before you sign' checklist for commercial/service agreements. Each
# item drives one retrieval query; callers may pass their own checklist.
DEFAULT_CHECKLIST: list[dict] = [
    {"id": "termination", "label": "Termination & notice",
     "query": "termination clause notice period how the agreement can be ended for convenience"},
    {"id": "liability", "label": "Liability & indemnity",
     "query": "limitation of liability indemnity cap on damages unlimited liability"},
    {"id": "auto_renewal", "label": "Automatic renewal",
     "query": "automatic renewal auto-renew evergreen term renewal notice"},
    {"id": "payment", "label": "Payment & late fees",
     "query": "payment terms fees invoicing late payment interest penalties"},
    {"id": "confidentiality", "label": "Confidentiality",
     "query": "confidentiality non-disclosure confidential information obligations"},
    {"id": "ip", "label": "Intellectual property",
     "query": "intellectual property ownership assignment licence of work product"},
    {"id": "data_protection", "label": "Data protection",
     "query": "data protection personal data processing GDPR sub-processors security"},
    {"id": "governing_law", "label": "Governing law & disputes",
     "query": "governing law jurisdiction dispute resolution arbitration venue"},
]

# The findings categories this mode emits. Registered in evidence.findings so
# create_finding() does not coerce them to 'Unsupported Finding'.
FLAG_CATEGORY = "Risky Clause"
MISSING_CATEGORY = "Missing Protection"
REVIEW_CATEGORY = "Requires Human Review"


def _build_prompt(item: dict, context: str) -> str:
    """Templated 'before you sign' review prompt for one checklist item."""
    return (
        "You are an evidence-bound contract-review assistant helping someone "
        "decide whether a document is safe to sign. Use ONLY the numbered "
        "CONTEXT passages below — do not add facts and do not give legal advice "
        "or state that any term is unlawful.\n\n"
        f"CHECKLIST ITEM: {item['label']} — {item['query']}\n\n"
        f"CONTEXT:\n{context}\n\n"
        "Decide, using only the context:\n"
        "- \"flag\": the document contains an unusual, one-sided, or risky term "
        "for this item.\n"
        "- \"missing\": the context does not show the expected protection for "
        "this item (a missing protection worth raising).\n"
        "- \"ok\": the context shows a reasonable, standard term.\n\n"
        "Respond with ONE JSON object only, no prose:\n"
        '{\"status\": \"flag|missing|ok\", \"explanation\": \"<=40 words, cite '
        'SRC- ids you used\", \"source_refs\": [\"SRC-...\"], \"confidence\": '
        '\"Low|Medium|High\"}'
    )


def _parse_model_json(text: str) -> Optional[dict]:
    """Defensively extract the first JSON object from a local-model reply."""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _sources_for(rows: list[dict], refs) -> list[RetrievedSource]:
    """Map source-ref IDs back to retrieved rows, dropping any not retrieved.

    This is the no-external-facts guardrail: a finding can only cite passages
    that were actually retrieved for this Task. Hallucinated refs are dropped.
    """
    by_ref = {r["source_ref"]: r for r in rows}
    picked = [by_ref[ref] for ref in (refs or []) if ref in by_ref]
    if not picked:
        picked = rows  # fall back to the whole retrieved set that was reviewed
    return [RetrievedSource.from_row(r) for r in picked]


def _degrade_finding(item: dict, rows: list[dict]) -> Optional[dict]:
    """Deterministic finding when the local model is unavailable — no fabrication."""
    if not rows:
        return {
            "title": f"{item['label']}: no matching evidence found",
            "category": MISSING_CATEGORY,
            "explanation": (
                f"No indexed passage addresses '{item['label']}'. This protection "
                "may be missing, or the documents may need processing. "
                + HUMAN_REVIEW_NOTE
            ),
            "sources": [],  # dropped by create_finding — surfaced via UI note instead
            "confidence": "Low",
            "confidence_reason": CONFIDENCE_REASON,
            "human_review_required": True,
        }
    return {
        "title": f"{item['label']}: review the retrieved clauses manually",
        "category": REVIEW_CATEGORY,
        "explanation": (
            "Local model unavailable — the passages below were retrieved as "
            f"relevant to '{item['label']}' but were not auto-assessed. "
            + HUMAN_REVIEW_NOTE
        ),
        "sources": [RetrievedSource.from_row(r) for r in rows],
        "confidence": "Low",
        "confidence_reason": CONFIDENCE_REASON,
        "human_review_required": True,
    }


def _finalize_sourceless(candidate: dict) -> dict:
    """Emit a sourceless 'missing protection' candidate in the finding shape.

    Mirrors create_finding's category/confidence validation and human-review
    defaulting, but keeps a finding whose evidence is a genuine *absence* — there
    is nothing to cite, so supporting_sources is empty by design.
    """
    category = candidate.get("category", "Unsupported Finding")
    if category not in VALID_CATEGORIES:
        category = "Unsupported Finding"
    confidence = candidate.get("confidence", "Low")
    if confidence not in VALID_CONFIDENCE:
        confidence = "Low"
    return {
        "title": candidate["title"],
        "category": category,
        "explanation": candidate["explanation"],
        "supporting_sources": [],
        "confidence": confidence,
        "confidence_reason": candidate.get("confidence_reason", CONFIDENCE_REASON),
        "human_review_required": bool(candidate.get("human_review_required", True)),
    }


class PolicyAuditor:
    """Runs a checklist ('before you sign') over a Task's local evidence."""

    def __init__(
        self,
        connector: Optional[OllamaConnector] = None,
        retriever: Optional[Retriever] = None,
        limit: int = 6,
    ):
        self.connector = connector or OllamaConnector()
        self.retriever = retriever or default_retriever
        self.limit = limit

    def _finding_for_item(self, item: dict, rows: list[dict]) -> Optional[dict]:
        context = build_context_block(rows)
        raw = self.connector.generate(_build_prompt(item, context))
        parsed = _parse_model_json(raw)
        if not parsed:
            # Model replied but we could not parse it — do not fabricate a verdict.
            return _degrade_finding(item, rows)

        status = str(parsed.get("status", "")).lower().strip()
        confidence = parsed.get("confidence", "Low")
        explanation = str(parsed.get("explanation", "")).strip() or (
            f"Automated screen for '{item['label']}'."
        )
        explanation = f"{explanation} {HUMAN_REVIEW_NOTE}"

        if status == "ok":
            return None  # 'before you sign' surfaces flags & gaps, not clean items
        if status == "missing":
            allowed = allowed_source_refs(rows)
            if not allowed:
                sources = []
            else:
                sources = _sources_for(rows, parsed.get("source_refs"))
            return {
                "title": f"{item['label']}: expected protection may be missing",
                "category": MISSING_CATEGORY,
                "explanation": explanation,
                "sources": sources,
                "confidence": confidence,
                "confidence_reason": CONFIDENCE_REASON,
                "human_review_required": True,
            }
        # default / "flag"
        return {
            "title": f"{item['label']}: potentially risky clause flagged",
            "category": FLAG_CATEGORY,
            "explanation": explanation,
            "sources": _sources_for(rows, parsed.get("source_refs")),
            "confidence": confidence,
            "confidence_reason": CONFIDENCE_REASON,
            "human_review_required": True,
        }

    def audit(
        self,
        matter_id: str,
        checklist: Optional[list[dict]] = None,
    ) -> list[dict]:
        checklist = checklist or DEFAULT_CHECKLIST
        model_available = self.connector.available()
        sourced: list[dict] = []
        # 'missing protection, nothing retrieved' findings have no source to cite
        # by nature — the findings model (correctly) drops sourceless candidates,
        # so we surface these directly rather than lose the most important signal
        # a before-you-sign review can raise.
        sourceless: list[dict] = []
        for item in checklist:
            rows = self.retriever(matter_id, item["query"], self.limit)
            if model_available:
                candidate = self._finding_for_item(item, rows)
            else:
                candidate = _degrade_finding(item, rows)
            if candidate is None:
                continue
            if candidate.get("sources"):
                sourced.append(candidate)
            else:
                sourceless.append(_finalize_sourceless(candidate))
        # Reuse the existing findings model for sourced candidates: validates
        # categories/confidence, dedups, and defaults human_review_required.
        return finalize_findings(sourced) + sourceless
