"""Jurisdiction-scoped law reference library + law-grounded retrieval.

RAYAAAA-251 (Phase C of RAYAAAA-241). Adds a per-jurisdiction corpus of statute /
regulation text (uploaded by the owner from OFFICIAL government publishers, per
the RAYAAAA-243 Counsel memo) and composes it into a Task's grounded retrieval so
a law-grounded answer is restricted to {linked client's state} UNION {federal}
law ONLY — never another state's — and can never emit a section citation that is
not backed by a retrieved chunk.

Public-domain law only: the corpus is keyed by JURISDICTION (a US state code or
``federal``), never by client_id / matter_id, so it is physically separate from
the client policy/document corpus and is NOT swept by client-data erasure.
"""

from review_engine.law.library import (
    FEDERAL_JURISDICTION,
    LAW_DISCLAIMER,
    LAW_JURISDICTION_CHOICES,
    LawLibraryIndex,
    LawProvenance,
    citation_stamp,
    enforce_law_citation_guardrail,
    is_valid_law_jurisdiction,
    law_jurisdiction_label,
    resolve_law_jurisdictions,
    validate_law_jurisdiction,
)

__all__ = [
    "FEDERAL_JURISDICTION",
    "LAW_DISCLAIMER",
    "LAW_JURISDICTION_CHOICES",
    "LawLibraryIndex",
    "LawProvenance",
    "citation_stamp",
    "enforce_law_citation_guardrail",
    "is_valid_law_jurisdiction",
    "law_jurisdiction_label",
    "resolve_law_jurisdictions",
    "validate_law_jurisdiction",
]
