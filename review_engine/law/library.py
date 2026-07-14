"""Jurisdiction-scoped law reference corpus and its counsel-binding guardrails.

Everything here implements the RAYAAAA-243 Counsel requirements for Phase C:

* **Jurisdiction hard-filter (AC C)** — the law corpus is *partitioned* by
  jurisdiction: each jurisdiction (a US state code, or ``federal``) gets its own
  physically separate Chroma store keyed by that jurisdiction, exactly like the
  RAYAAAA-245 per-client policy library is keyed by client id. A Task resolves
  its linked client's state and instantiates ONLY ``{state} ∪ {federal}`` law
  indexes, so another state's law store is never even opened — isolation is
  structural, not a post-query filter.
* **Mandatory provenance (AC B)** — :class:`LawProvenance` carries the four
  required fields (source name, source URL, effective version, retrieval date);
  it is attached to every stored law chunk's metadata so each retrieved chunk can
  render its own per-citation provenance stamp (AC F).
* **No-citation-without-retrieved-chunk (AC E)** —
  :func:`enforce_law_citation_guardrail` is an unconditional post-generation
  sanitizer: any statute/section citation in an answer that is not literally
  backed by retrieved law text is replaced with ``not in reference library``.
  This runs on every law-grounded answer, so a citation fabricated from the
  model's training data can never reach the user as authoritative.
* **Disclaimer (AC G)** — :data:`LAW_DISCLAIMER` is the verbatim paragraph from
  RAYAAAA-243 memo §2, rendered on every law-grounded answer.

Public-domain law only; SYNTHETIC / owner-internal until the Phase 4 gate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from review_engine.clients.jurisdictions import (
    STATE_NAMES,
    UNSPECIFIED_STATE,
    state_label,
)
from review_engine.config.settings import LAW_INDEXES_DIR
from review_engine.evidence.index import EvidenceIndex

# Canonical partition key for United States FEDERAL law. Distinct from the Phase-A
# client jurisdiction sentinel ``UNSPECIFIED_STATE`` ("US"): a client's state may
# be "unspecified/federal", but the law corpus is keyed by an explicit ``federal``
# partition so the resolution of "state ∪ federal" is always unambiguous.
FEDERAL_JURISDICTION = "federal"

# Collection namespace for the law corpus. Distinct from "matter" (Task index)
# and "policy" (client policy library) so the three corpora can never collide.
LAW_COLLECTION_PREFIX = "law"

# Upload picker order: federal first (default), then the 50 states + DC A–Z.
LAW_JURISDICTION_CHOICES: list[str] = [FEDERAL_JURISDICTION] + sorted(STATE_NAMES)


# --- The exact required disclaimer (RAYAAAA-243 memo §2, verbatim) -----------
# Rendered on EVERY law-grounded chat answer and any audit/report output that
# relies on law grounding (AC G). Do NOT paraphrase — this text is counsel-bound.
LAW_DISCLAIMER = (
    "Not legal advice. This response is an automated document-review aid. It "
    "surfaces text from law/regulation documents that were uploaded to this "
    "Task's reference library; it does not independently verify that this text "
    "is current, complete, or the controlling authority for your situation. "
    "Statutes and regulations change and may have been amended, repealed, or "
    "superseded after the reference text was added (the source and date are "
    "shown with each citation). This tool does not create an attorney-client "
    "relationship and is not a substitute for review by a licensed attorney in "
    "the applicable jurisdiction. Verify all cited authority against the "
    "official source before relying on it."
)

# Phrase substituted for any citation the guardrail cannot back with a chunk.
NOT_IN_LIBRARY = "not in reference library"


def law_jurisdiction_label(code: str) -> str:
    """Human label for a law jurisdiction code (``federal`` or a state code)."""
    token = (code or "").strip()
    if token.lower() == FEDERAL_JURISDICTION:
        return "Federal (United States)"
    return state_label(token)


def normalize_law_jurisdiction(value: str | None) -> str | None:
    """Return the canonical law-jurisdiction key for ``value`` or ``None``.

    Accepts ``federal`` (any case) or a US state code/name. Note that the Phase-A
    client sentinel "US" (unspecified/federal) maps to ``federal`` here.
    """
    if value is None:
        return None
    token = value.strip()
    if not token:
        return None
    if token.lower() == FEDERAL_JURISDICTION:
        return FEDERAL_JURISDICTION
    upper = token.upper()
    if upper == UNSPECIFIED_STATE:
        return FEDERAL_JURISDICTION
    if upper in STATE_NAMES:
        return upper
    # Allow a full state name (e.g. "California").
    for code, name in STATE_NAMES.items():
        if name.lower() == token.lower():
            return code
    return None


def is_valid_law_jurisdiction(value: str | None) -> bool:
    return normalize_law_jurisdiction(value) is not None


def validate_law_jurisdiction(value: str | None) -> str:
    """Return the canonical law jurisdiction or raise ``ValueError``."""
    code = normalize_law_jurisdiction(value)
    if code is None:
        raise ValueError(
            f"Unknown law jurisdiction {value!r}; expected {FEDERAL_JURISDICTION!r} "
            "or a US state code/name."
        )
    return code


def resolve_law_jurisdictions(client_state: str | None) -> list[str]:
    """The law partitions a Task may retrieve: ``{client state} ∪ {federal}``.

    This is the AC-C hard filter expressed as a partition set. Federal law is
    ALWAYS included. A concrete client state (not the "US" unspecified sentinel)
    adds exactly that one state — never any other. The composed retriever opens
    only the returned partitions, so a State-A Task never touches State-B's store.
    """
    juris: list[str] = [FEDERAL_JURISDICTION]
    code = (client_state or "").strip().upper()
    if code and code != UNSPECIFIED_STATE and code in STATE_NAMES:
        juris.append(code)
    return juris


@dataclass(frozen=True)
class LawProvenance:
    """Mandatory provenance for one uploaded law document (AC B).

    All four fields are required; :meth:`validate` rejects a document missing any
    of them so the upload path can never index unattributed law text.
    """

    source_name: str
    source_url: str
    effective: str  # effective date / version
    retrieved: str  # retrieval date (YYYY-MM-DD)

    REQUIRED = ("source_name", "source_url", "effective", "retrieved")

    def validate(self) -> "LawProvenance":
        missing = [
            field
            for field in self.REQUIRED
            if not str(getattr(self, field) or "").strip()
        ]
        if missing:
            raise ValueError(
                "Law document rejected — missing required provenance: "
                + ", ".join(missing)
            )
        return self

    def as_metadata(self) -> dict:
        """Provenance keys attached to each stored law chunk's metadata.

        Prefixed with ``law_`` so they never collide with the base index metadata
        keys (document_name / section / citation / …)."""
        return {
            "law_source_name": self.source_name.strip(),
            "law_source_url": self.source_url.strip(),
            "law_effective": self.effective.strip(),
            "law_retrieved": self.retrieved.strip(),
        }

    def stamp(self) -> str:
        return citation_stamp(self.source_name, self.retrieved, self.effective)


def citation_stamp(source_name: str, retrieved: str, effective: str) -> str:
    """The AC-F per-citation provenance stamp."""
    return (
        f"[Source: {(source_name or '?').strip()}, "
        f"retrieved {(retrieved or '?').strip()}, "
        f"effective {(effective or '?').strip()}]"
    )


class LawLibraryIndex(EvidenceIndex):
    """A single jurisdiction's law corpus, physically isolated per jurisdiction.

    ``jurisdiction`` (a state code or ``federal``) is used exactly where
    ``EvidenceIndex`` uses ``matter_id`` — as the storage key and source-reference
    salt — but rooted at ``LAW_INDEXES_DIR`` with the ``law`` collection prefix so
    a jurisdiction's law can never share a store with a Task's documents, a
    client's policy library, or another jurisdiction's law.
    """

    def __init__(self, jurisdiction: str, root: str | Path = LAW_INDEXES_DIR):
        canonical = validate_law_jurisdiction(jurisdiction)
        super().__init__(
            canonical, root=root, collection_prefix=LAW_COLLECTION_PREFIX
        )
        self.jurisdiction = canonical

    @classmethod
    def for_jurisdiction(cls, jurisdiction: str) -> "LawLibraryIndex":
        return cls(jurisdiction)

    def build_with_provenance(self, chunks, provenance_by_document: dict) -> int:
        """Build the index, attaching each document's provenance to its chunks.

        ``provenance_by_document`` maps a document name to a
        :class:`LawProvenance`; the provenance metadata rides on every chunk so a
        retrieved law row carries its own citation-provenance stamp.
        """

        def metadata_extra(chunk):
            prov = provenance_by_document.get(chunk.document_name)
            return prov.as_metadata() if prov else {}

        return self.build(chunks, metadata_extra=metadata_extra)


# --- No-citation-without-retrieved-chunk guardrail (AC E) --------------------
#
# Detects statute/section citations in generated text and verifies each is backed
# by retrieved law text. Anything unbacked is redacted to ``not in reference
# library``. Fail-closed: if a citation core cannot be located in the retrieved
# corpus, it is treated as fabricated and redacted.

# Matches the numeric/alphanumeric "core" of a citation after a §, "Section",
# "Sec.", or a U.S.C./C.F.R. reporter marker. The captured group is the section
# identifier we verify against retrieved text.
_CITATION_RE = re.compile(
    r"(?:§+\s*"
    r"|(?:\bsections?\b|\bsec\.)\s+"
    r"|(?:\b\d+\s+)?(?:U\.?\s?S\.?\s?C\.?|C\.?\s?F\.?\s?R\.?)\s*§?\s*)"
    r"(\d[\w.\-]*)",
    re.IGNORECASE,
)


def _normalize_core(core: str) -> str:
    return (core or "").strip().lower().rstrip(".,;:)")


def _corpus_text(law_rows: list[dict]) -> str:
    parts = []
    for row in law_rows:
        parts.append(str(row.get("text", "")))
        parts.append(str(row.get("section", "")))
        parts.append(str(row.get("citation", "")))
    return " \n ".join(parts).lower()


def _is_backed(core: str, corpus: str) -> bool:
    if not core:
        return False
    # Digit-boundary match so "§ 12" is NOT considered backed by "512".
    return re.search(r"(?<!\d)" + re.escape(core) + r"(?!\d)", corpus) is not None


def enforce_law_citation_guardrail(
    answer_text: str, law_rows: list[dict]
) -> tuple[str, list[str]]:
    """Redact any statute/section citation not backed by a retrieved law chunk.

    Returns ``(sanitized_text, redacted_citations)``. Called unconditionally on
    every law-grounded answer, so it is technically impossible for a citation
    absent from the retrieval set to survive in the displayed answer — it is
    replaced with the literal phrase ``not in reference library``.
    """
    if not answer_text:
        return answer_text or "", []
    corpus = _corpus_text(law_rows)
    redacted: list[str] = []
    out = answer_text
    # Replace right-to-left so earlier match spans stay valid.
    for match in reversed(list(_CITATION_RE.finditer(answer_text))):
        core = _normalize_core(match.group(1))
        if _is_backed(core, corpus):
            continue
        redacted.append(match.group(0).strip())
        out = out[: match.start()] + NOT_IN_LIBRARY + out[match.end() :]
    return out, list(reversed(redacted))
