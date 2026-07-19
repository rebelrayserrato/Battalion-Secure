"""Pending-Review staging for web-fetched law (RAYAAAA-274 P2).

Counsel Condition D / CTO Condition 5: web-fetched law is written to a
**Pending Review staging area — NEVER the live index**. Owner approval (the
RAYAAAA-275 "Law Library → Pending Review" UI) is what moves a staged document
into the live RAYAAAA-251 ``LawLibraryIndex``; auto-add is forbidden.

This module is the *producer* side P2 owns: it turns a fetched+extracted document
into a :class:`StagedLawDocument` record and writes it, via a :class:`StagingSink`,
into ``LAW_STAGING_DIR``. The default :class:`WebLawStagingStore` is a file-backed
sink whose on-disk record is exactly what the RAYAAAA-275 review UI reads,
approves (→ 251 provenance-enforced upload), or rejects (→ discard). The
:class:`StagingSink` protocol is the seam at which the RAYAAAA-275
``LawStagingStore`` is folded in at the 270 cutover.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from review_engine.config.settings import LAW_STAGING_DIR
from review_engine.extraction.models import SourceChunk
from review_engine.law.library import validate_law_jurisdiction
from review_engine.law.web.provenance import WebLawProvenance


@dataclass(frozen=True)
class StagedLawDocument:
    """One web-fetched law document awaiting owner review (status ``pending``)."""

    jurisdiction: str
    document_name: str
    chunks: tuple[SourceChunk, ...]
    provenance: WebLawProvenance
    staged_at: str = ""
    status: str = "pending"
    dropped_annotation_count: int = 0

    def to_dict(self) -> dict:
        return {
            "jurisdiction": self.jurisdiction,
            "document_name": self.document_name,
            "status": self.status,
            "staged_at": self.staged_at,
            "dropped_annotation_count": self.dropped_annotation_count,
            "provenance": {
                "source_name": self.provenance.source_name,
                "source_url": self.provenance.source_url,
                "effective": self.provenance.effective,
                "retrieved": self.provenance.retrieved,
                "source_system": self.provenance.source_system,
                "official_source": self.provenance.official_source,
                "content_type": self.provenance.content_type,
                "contained_annotations": self.provenance.contained_annotations,
            },
            "chunks": [c.to_dict() for c in self.chunks],
        }


@runtime_checkable
class StagingSink(Protocol):
    """Persists a staged document for owner review; returns a staging id.

    An implementation MUST NOT write to the live law index — staging is a
    holding area only. This is the seam the RAYAAAA-275 ``LawStagingStore`` plugs
    into at cutover."""

    def stage(self, record: StagedLawDocument) -> str: ...


class WebLawStagingStore:
    """File-backed Pending-Review store under ``LAW_STAGING_DIR``.

    Never touches ``LAW_INDEXES_DIR``. Records are grouped by jurisdiction so the
    RAYAAAA-251 jurisdiction hard-filter is preserved on disk too (a staged
    document lives under its own jurisdiction's folder and nowhere else).
    """

    def __init__(self, root: str | Path = LAW_STAGING_DIR):
        self.root = Path(root)

    def _dir(self, jurisdiction: str) -> Path:
        canonical = validate_law_jurisdiction(jurisdiction)
        return self.root / canonical

    def stage(self, record: StagedLawDocument) -> str:
        # Re-validate provenance at the boundary: nothing un-attributed or
        # non-official can be persisted even if a caller bypassed the pipeline.
        record.provenance.validate()
        target_dir = self._dir(record.jurisdiction)
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        safe_name = "".join(
            ch if ch.isalnum() or ch in "-_." else "_" for ch in record.document_name
        )[:80]
        staging_id = f"{record.jurisdiction}/{stamp}_{safe_name}"
        path = target_dir / f"{stamp}_{safe_name}.json"
        payload = record.to_dict()
        payload["staging_id"] = staging_id
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return staging_id

    def list_pending(self, jurisdiction: str | None = None) -> list[dict]:
        records: list[dict] = []
        roots = [self._dir(jurisdiction)] if jurisdiction else (
            [p for p in self.root.glob("*") if p.is_dir()] if self.root.exists() else []
        )
        for d in roots:
            if not d.exists():
                continue
            for path in sorted(d.glob("*.json")):
                try:
                    rec = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if rec.get("status", "pending") == "pending":
                    records.append(rec)
        return records

    def discard(self, staging_id: str) -> bool:
        """Reject a staged document (RAYAAAA-275 'Reject' → discard)."""
        path = self._path_for(staging_id)
        if path and path.exists():
            path.unlink()
            return True
        return False

    def _path_for(self, staging_id: str) -> Path | None:
        try:
            jurisdiction, name = staging_id.split("/", 1)
        except ValueError:
            return None
        return self._dir(jurisdiction) / f"{name}.json"
