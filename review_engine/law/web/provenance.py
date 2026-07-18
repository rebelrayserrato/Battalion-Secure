"""Provenance for web-fetched law (RAYAAAA-274 P2), extending RAYAAAA-251.

RAYAAAA-251 already requires four provenance fields on every stored law chunk
(:class:`~review_engine.law.library.LawProvenance`: source name / URL / effective
version / retrieval date). Web ingest adds three more the counsel/CTO conditions
require:

* ``source_system``  — which official API (govinfo / congress / ecfr) it came from;
* ``official_source`` — True only when the fetch host was an allowlisted official
  publisher (the ONLY case P2 permits);
* ``content_type``   — the Counsel-B statutory-vs-annotated flag (P2 ingests
  ``statutory`` only, but the flag is stamped so a reviewer/audit can see it, and
  ``contained_annotations`` records that apparatus was found and stripped).

We compose (not subclass) the frozen 251 ``LawProvenance`` so its ``validate()``
still gates all four base fields, then merge the extra keys into ``as_metadata``.
"""
from __future__ import annotations

from dataclasses import dataclass

from review_engine.law.library import LawProvenance, citation_stamp


@dataclass(frozen=True)
class WebLawProvenance:
    """Full provenance for one web-fetched, staged law document."""

    source_name: str
    source_url: str
    effective: str
    retrieved: str
    source_system: str
    official_source: bool
    content_type: str = "statutory"
    contained_annotations: bool = False

    def _base(self) -> LawProvenance:
        return LawProvenance(
            source_name=self.source_name,
            source_url=self.source_url,
            effective=self.effective,
            retrieved=self.retrieved,
        )

    def validate(self) -> "WebLawProvenance":
        # Reuse the 251 required-field gate for the four base fields.
        self._base().validate()
        if not str(self.source_system or "").strip():
            raise ValueError("web law document rejected — missing source_system")
        if not self.official_source:
            # P2 refuses to stage anything not fetched from an official publisher.
            raise ValueError(
                "web law document rejected — not from an official source "
                "(only allowlisted government publishers may be ingested)"
            )
        if self.content_type != "statutory":
            raise ValueError(
                f"web law document rejected — content_type {self.content_type!r} "
                "is not 'statutory' (statutory text only, per Counsel Condition B)"
            )
        return self

    def as_metadata(self) -> dict:
        meta = self._base().as_metadata()
        meta.update(
            {
                "law_source_system": self.source_system.strip(),
                "law_official_source": bool(self.official_source),
                "law_content_type": self.content_type,
                "law_contained_annotations": bool(self.contained_annotations),
            }
        )
        return meta

    def stamp(self) -> str:
        return citation_stamp(self.source_name, self.retrieved, self.effective)
