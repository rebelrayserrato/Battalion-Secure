"""The web-law ingest pipeline (RAYAAAA-274 P2) — fetch → extract → stage.

Orchestrates a single ingest:

1. **Flag guard** — if ``LAW_WEB_INGEST_ENABLED`` is off, refuse to run
   (:class:`FeatureDisabled`). Nothing is fetched, nothing is built. INERT.
2. **No-PII query** — validate the :class:`LawQuery` (structured citation only).
3. **Fetch** — the source adapter builds an allowlisted https URL and fetches via
   the injected proxy-bound transport (RAYAAAA-273 egress proxy).
4. **Jurisdiction hard-filter** — assert the fetched document's jurisdiction ==
   the query's; a mismatch is a :class:`JurisdictionLeak` and aborts (RAYAAAA-251
   partitioning is preserved — a document is filed under its own jurisdiction,
   never cross-filed).
5. **Statutory extraction** — keep statutory text only; drop + flag annotations
   (Counsel B).
6. **Chunk + provenance** — chunk the statutory text (keyed by jurisdiction, like
   the 251 law index) and attach full :class:`WebLawProvenance`.
7. **Stage** — write to the Pending-Review sink; NEVER the live index (Counsel D /
   CTO 5). Owner approval (RAYAAAA-275) is required to go live.

SYNTHETIC / owner-internal only; does not advance the Phase-4 real-PII gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from review_engine.config import settings
from review_engine.extraction.extractors import _split_text
from review_engine.extraction.models import SourceChunk, source_reference
from review_engine.law.web.adapters import HttpTransport, RawLawDocument, adapter_for
from review_engine.law.web.extraction import extract_statutory
from review_engine.law.web.provenance import WebLawProvenance
from review_engine.law.web.query import LawQuery
from review_engine.law.web.staging import (
    StagedLawDocument,
    StagingSink,
    WebLawStagingStore,
)


class FeatureDisabled(RuntimeError):
    """Raised when the pipeline is invoked while ``LAW_WEB_INGEST_ENABLED`` is off."""


class JurisdictionLeak(RuntimeError):
    """Raised when a fetched document's jurisdiction != the requested one."""


class EmptyStatutoryText(ValueError):
    """Raised when nothing statutory remains after extraction (nothing to stage)."""


@dataclass(frozen=True)
class IngestResult:
    staging_id: str
    jurisdiction: str
    document_name: str
    source_system: str
    source_url: str
    chunk_count: int
    contained_annotations: bool
    dropped_annotation_count: int


class WebLawIngestPipeline:
    """Fetch one official-source law document and stage it for owner review."""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        staging_sink: StagingSink | None = None,
        enabled: bool | None = None,
    ):
        self.transport = transport
        self.staging_sink = staging_sink or WebLawStagingStore()
        # Read the flag at construction, defaulting to the live setting; an
        # explicit ``enabled`` is only for tests exercising the ON path.
        self._enabled = settings.LAW_WEB_INGEST_ENABLED if enabled is None else enabled

    def ingest(self, query: LawQuery) -> IngestResult:
        if not self._enabled:
            raise FeatureDisabled(
                "law web ingest is disabled (LAW_WEB_INGEST_ENABLED off) — "
                "this pipeline is INERT until the 270 gate is green"
            )

        query = query.validated()
        adapter = adapter_for(query.source_system)
        raw: RawLawDocument = adapter.fetch(query, self.transport)

        # --- Jurisdiction hard-filter (RAYAAAA-251 AC-C preserved) ----------
        if raw.jurisdiction != query.jurisdiction:
            raise JurisdictionLeak(
                f"fetched jurisdiction {raw.jurisdiction!r} != requested "
                f"{query.jurisdiction!r} — refusing to cross-file"
            )

        # --- Statutory-only extraction (Counsel B) --------------------------
        extraction = extract_statutory(raw.text)
        if not extraction.statutory_text.strip():
            raise EmptyStatutoryText(
                f"no statutory text extracted from {raw.source_url!r} "
                f"({len(extraction.dropped_annotations)} annotation blocks dropped)"
            )

        document_name = self._document_name(raw)

        # --- Chunk (keyed by jurisdiction, like the 251 law index) ----------
        chunks = self._chunk(raw.jurisdiction, document_name, extraction.statutory_text)

        # --- Provenance (four 251 fields + web fields) ----------------------
        provenance = WebLawProvenance(
            source_name=self._source_name(raw),
            source_url=raw.source_url,
            effective=raw.effective or "unspecified",
            retrieved=raw.retrieved,
            source_system=raw.source_system,
            official_source=raw.official_source,
            content_type=extraction.content_type,
            contained_annotations=extraction.contained_annotations,
        ).validate()

        record = StagedLawDocument(
            jurisdiction=raw.jurisdiction,
            document_name=document_name,
            chunks=tuple(chunks),
            provenance=provenance,
            staged_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            status="pending",
            dropped_annotation_count=len(extraction.dropped_annotations),
        )

        # --- Stage ONLY — never the live index (Counsel D / CTO 5) ----------
        staging_id = self.staging_sink.stage(record)

        return IngestResult(
            staging_id=staging_id,
            jurisdiction=raw.jurisdiction,
            document_name=document_name,
            source_system=raw.source_system,
            source_url=raw.source_url,
            chunk_count=len(chunks),
            contained_annotations=extraction.contained_annotations,
            dropped_annotation_count=len(extraction.dropped_annotations),
        )

    # -- helpers --------------------------------------------------------------
    @staticmethod
    def _document_name(raw: RawLawDocument) -> str:
        base = (raw.citation or raw.title or "law").strip()
        return f"{base} [{raw.source_system}]"

    @staticmethod
    def _source_name(raw: RawLawDocument) -> str:
        labels = {
            "govinfo": "GPO govinfo (official)",
            "congress": "Congress.gov (Library of Congress, official)",
            "ecfr": "eCFR (official)",
        }
        return labels.get(raw.source_system, raw.source_system)

    @staticmethod
    def _chunk(jurisdiction: str, document_name: str, text: str) -> list[SourceChunk]:
        chunks: list[SourceChunk] = []
        for ordinal, part in enumerate(_split_text(text)):
            if not part.strip():
                continue
            chunks.append(
                SourceChunk(
                    # The law corpus uses ``jurisdiction`` exactly where a Task
                    # index uses ``matter_id`` (see LawLibraryIndex), so the
                    # staged chunks are already keyed for the 251 upload path.
                    matter_id=jurisdiction,
                    document_name=document_name,
                    file_type="law/web",
                    text=part,
                    section="body",
                    source_ref=source_reference(
                        jurisdiction, document_name, section="body", ordinal=ordinal
                    ),
                )
            )
        return chunks
