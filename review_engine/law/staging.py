"""The "Pending Review" staging area for web-fetched law (RAYAAAA-270 P3 / RAYAAAA-275).

This is the holding pen that sits BETWEEN the web-ingest pipeline (RAYAAAA-274 P2,
which fetches statute/regulation text from official government publishers) and the
live RAYAAAA-251 law index. It exists to enforce the single most important rule of
the whole RAYAAAA-270 feature:

    **Auto-add is FORBIDDEN.** Nothing a machine fetched from the web ever lands in
    the live, citable law index without an explicit owner Approve. (RAYAAAA-243 /
    Counsel Cond A–E on RAYAAAA-271 + CTO condition 5 on RAYAAAA-272.)

The seam is deliberately narrow:

* **P2 (RAYAAAA-274) writes** by calling :meth:`LawStagingStore.stage` once per
  fetched document. That is the ONLY way a record enters the queue. ``stage`` does
  no indexing and no egress — it just parks a :class:`PendingLaw` on disk.
* **P3 (this + the Law Library UI) reads** the queue and offers the owner exactly
  two terminal actions per record:
  * :meth:`LawStagingStore.approve` — pushes the record through the EXISTING
    RAYAAAA-251 provenance-enforced upload (``ReviewService.save_law_upload`` +
    ``process_law_library``) into the live index, then removes it from the queue.
  * :meth:`LawStagingStore.reject` — discards the record.
  Either way an append-only audit line is written, so every owner decision (and the
  provenance it acted on) is recoverable.

A staged record is NEVER retrievable or citable: it lives under ``LAW_STAGING_DIR``,
which no retriever ever opens, entirely apart from ``LAW_UPLOADS_DIR`` / the live
law indexes. Approve is the one and only bridge, and it is a manual owner click.

Defense in depth on Counsel conditions: :meth:`approve` itself refuses to promote a
record that is not from an official source (Cond A) or that is annotated rather than
pure statutory text (Cond B) — so even a mis-staged record cannot be clicked into the
live index. Synthetic / owner-internal only until the Phase-4 real-PII gate.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from review_engine.config.settings import LAW_STAGING_DIR
from review_engine.law.library import law_jurisdiction_label, validate_law_jurisdiction

# Preview length for the extracted-text snippet shown in the queue row.
PREVIEW_CHARS = 600


class StagingApprovalError(RuntimeError):
    """Raised when a staged record fails the counsel-binding approval guards."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class PendingLaw:
    """One web-fetched law document awaiting the owner's Approve / Reject decision.

    The provenance fields mirror :class:`review_engine.law.library.LawProvenance`
    (all four are required before Approve can call the RAYAAAA-251 upload) plus the
    two owner-facing signals the P3 acceptance criteria require the queue to show:
    ``official_source`` (the official-source badge) and ``statutory_only`` (the
    statutory-vs-annotated flag). ``text`` is the already-extracted, statutory-only
    text the P2 pipeline produced; ``provenance_extra`` carries any additional P2
    provenance (e.g. API-vs-scrape, robots/ToS compliance markers) for display and
    forward compatibility without widening this contract.
    """

    id: str
    jurisdiction: str
    source_url: str
    source_name: str
    retrieved: str  # retrieval date (YYYY-MM-DD)
    effective: str  # effective date / version
    text: str
    official_source: bool = False
    statutory_only: bool = False
    suggested_filename: str = ""
    staged_at: str = ""
    provenance_extra: dict = field(default_factory=dict)

    @property
    def jurisdiction_label(self) -> str:
        try:
            return law_jurisdiction_label(self.jurisdiction)
        except Exception:
            return self.jurisdiction

    @property
    def text_preview(self) -> str:
        snippet = (self.text or "").strip()
        if len(snippet) <= PREVIEW_CHARS:
            return snippet
        return snippet[:PREVIEW_CHARS].rstrip() + "…"

    def filename(self) -> str:
        """A safe ``.txt`` document name for the RAYAAAA-251 upload.

        Staged records hold already-extracted statutory text, so they are promoted
        as ``.txt`` (a RAYAAAA-251 SUPPORTED_EXTENSIONS type); the source format is
        preserved in provenance, not in the stored artifact.
        """
        stem = (self.suggested_filename or "").strip()
        if stem.lower().endswith(".txt"):
            return stem
        if stem:
            return f"{Path(stem).stem}.txt"
        return f"web-law-{self.id}.txt"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PendingLaw":
        known = {f: data.get(f) for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        known["provenance_extra"] = data.get("provenance_extra") or {}
        return cls(**known)


class LawStagingStore:
    """Disk-backed queue of :class:`PendingLaw` records + an append-only audit log.

    Layout under ``root``::

        <root>/pending/<id>.json   one queued record (metadata + extracted text)
        <root>/audit.log           append-only JSONL of every approve/reject decision

    The store is intentionally tiny and dependency-free (no DB, no network): it is a
    holding area, not a corpus. It never indexes or retrieves anything itself — only
    :meth:`approve` bridges to the live RAYAAAA-251 upload.
    """

    def __init__(self, root: str | Path = LAW_STAGING_DIR):
        self.root = Path(root)
        self.pending_dir = self.root / "pending"
        self.audit_path = self.root / "audit.log"
        self.pending_dir.mkdir(parents=True, exist_ok=True)

    # --- P2 (RAYAAAA-274) write seam ---------------------------------------
    def stage(
        self,
        *,
        jurisdiction: str,
        source_url: str,
        source_name: str,
        retrieved: str,
        effective: str,
        text: str,
        official_source: bool = False,
        statutory_only: bool = False,
        suggested_filename: str = "",
        provenance_extra: dict | None = None,
    ) -> str:
        """Park one fetched law document in the queue and return its id.

        This is the ONLY entry point into the queue and it performs no egress and no
        indexing — it just writes the record to disk for the owner to review. It does
        NOT auto-approve anything.
        """
        canonical = validate_law_jurisdiction(jurisdiction)
        record = PendingLaw(
            id=uuid.uuid4().hex[:12],
            jurisdiction=canonical,
            source_url=(source_url or "").strip(),
            source_name=(source_name or "").strip(),
            retrieved=(retrieved or "").strip(),
            effective=(effective or "").strip(),
            text=text or "",
            official_source=bool(official_source),
            statutory_only=bool(statutory_only),
            suggested_filename=(suggested_filename or "").strip(),
            staged_at=_utc_now(),
            provenance_extra=dict(provenance_extra or {}),
        )
        self._write(record)
        return record.id

    # --- P3 read + decide ---------------------------------------------------
    def list_pending(self) -> list[PendingLaw]:
        records = []
        for path in self.pending_dir.glob("*.json"):
            try:
                records.append(PendingLaw.from_dict(json.loads(path.read_text("utf-8"))))
            except (json.JSONDecodeError, OSError, TypeError):
                continue
        records.sort(key=lambda r: r.staged_at)
        return records

    def get(self, record_id: str) -> PendingLaw | None:
        path = self._path(record_id)
        if not path.exists():
            return None
        try:
            return PendingLaw.from_dict(json.loads(path.read_text("utf-8")))
        except (json.JSONDecodeError, OSError, TypeError):
            return None

    def pending_count(self) -> int:
        return len(list(self.pending_dir.glob("*.json")))

    def approve(self, record_id: str, svc, *, decided_by: str) -> dict:
        """Promote a staged record into the live RAYAAAA-251 law index.

        Runs the counsel-binding guards, then routes the record through the EXISTING
        provenance-enforced upload (``svc.save_law_upload`` — which re-validates all
        four provenance fields — followed by ``svc.process_law_library`` to index it).
        On success the record is removed from the queue and an audit line is written.
        Raises :class:`StagingApprovalError` if the record is missing or fails a guard.
        """
        record = self.get(record_id)
        if record is None:
            raise StagingApprovalError(f"No pending law record {record_id!r}.")
        # Cond A (RAYAAAA-271): only OFFICIAL government sources may enter the corpus.
        if not record.official_source:
            raise StagingApprovalError(
                "Cannot approve: source is not marked as an official government "
                "publisher (Counsel Cond A). Reject it instead."
            )
        # Cond B (RAYAAAA-271): statutory text ONLY — no West/Lexis-style annotations.
        if not record.statutory_only:
            raise StagingApprovalError(
                "Cannot approve: record is flagged as annotated, not pure statutory "
                "text (Counsel Cond B). Reject it instead."
            )
        filename = record.filename()
        svc.save_law_upload(
            record.jurisdiction,
            filename,
            record.text.encode("utf-8"),
            source_name=record.source_name,
            source_url=record.source_url,
            effective=record.effective,
            retrieved=record.retrieved,
        )
        result = svc.process_law_library(record.jurisdiction)
        self._audit(
            action="approve",
            record=record,
            decided_by=decided_by,
            detail={"filename": filename, "indexed_chunks": result.get("chunks")},
        )
        self._delete(record_id)
        return {
            "jurisdiction": record.jurisdiction,
            "filename": filename,
            "processed": result.get("processed"),
            "chunks": result.get("chunks"),
        }

    def reject(self, record_id: str, *, decided_by: str, reason: str = "") -> None:
        """Discard a staged record; nothing reaches the live index. Audited."""
        record = self.get(record_id)
        if record is None:
            raise StagingApprovalError(f"No pending law record {record_id!r}.")
        self._audit(
            action="reject",
            record=record,
            decided_by=decided_by,
            detail={"reason": (reason or "").strip()},
        )
        self._delete(record_id)

    def audit_entries(self, limit: int = 50) -> list[dict]:
        """Most-recent-first owner decisions, for the audit-trail panel."""
        if not self.audit_path.exists():
            return []
        lines = self.audit_path.read_text("utf-8").splitlines()
        entries = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        entries.reverse()
        return entries[:limit]

    # --- internals ----------------------------------------------------------
    def _path(self, record_id: str) -> Path:
        # Guard against path traversal via a crafted id.
        safe = "".join(c for c in (record_id or "") if c.isalnum() or c in "-_")
        return self.pending_dir / f"{safe}.json"

    def _write(self, record: PendingLaw) -> None:
        self._path(record.id).write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2), "utf-8"
        )

    def _delete(self, record_id: str) -> None:
        path = self._path(record_id)
        if path.exists():
            path.unlink()

    def _audit(self, *, action: str, record: PendingLaw, decided_by: str, detail: dict) -> None:
        entry = {
            "at": _utc_now(),
            "action": action,
            "decided_by": decided_by or "owner",
            "record_id": record.id,
            "jurisdiction": record.jurisdiction,
            "source_url": record.source_url,
            "source_name": record.source_name,
            "effective": record.effective,
            "retrieved": record.retrieved,
            "official_source": record.official_source,
            "statutory_only": record.statutory_only,
            "detail": detail,
        }
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


class LawStagingSink:
    """The P2↔P3 seam (RAYAAAA-287): adapt the RAYAAAA-274 pipeline's
    :class:`~review_engine.law.web.staging.StagingSink` protocol onto the
    RAYAAAA-275 :class:`LawStagingStore`.

    Before this, the pipeline defaulted to :class:`WebLawStagingStore`, which
    parks records under ``LAW_STAGING_DIR/<jurisdiction>/*.json`` — a *different*
    layout from the ``LAW_STAGING_DIR/pending/*.json`` queue the owner Pending
    Review UI (:meth:`LawStagingStore.list_pending`) actually reads, so
    web-fetched law never showed up for the owner. Injecting this sink makes the
    pipeline write into the SINGLE store the UI reads — one store, one directory.

    Mapping :class:`~review_engine.law.web.staging.StagedLawDocument` →
    :class:`PendingLaw`:

    * The pipeline has already run statutory-only extraction, so its provenance
      ``content_type`` is invariably ``"statutory"`` (:meth:`WebLawProvenance.validate`
      rejects anything else); we surface that as ``statutory_only=True`` so
      :meth:`LawStagingStore.approve` Cond B passes. ``contained_annotations``
      (apparatus that was *found and stripped*) is preserved in ``provenance_extra``
      for the audit, but does not block approval — the stored text is pure statute.
    * ``official_source`` carries the allowlisted-publisher fact the pipeline
      validated; :meth:`approve` Cond A re-checks it before any RAYAAAA-251 upload.

    Auto-add stays FORBIDDEN: this only *stages* — the owner must still Approve.
    """

    def __init__(self, store: "LawStagingStore | None" = None):
        self.store = store or LawStagingStore()

    def stage(self, record) -> str:
        prov = record.provenance
        # The staged chunks already hold ONLY statutory text (annotations were
        # dropped in extraction); re-join them into the document the owner will
        # promote verbatim into the 251 index.
        text = "\n\n".join(c.text for c in record.chunks)
        return self.store.stage(
            jurisdiction=record.jurisdiction,
            source_url=prov.source_url,
            source_name=prov.source_name,
            retrieved=prov.retrieved,
            effective=prov.effective,
            text=text,
            official_source=bool(prov.official_source),
            statutory_only=(prov.content_type == "statutory"),
            suggested_filename=record.document_name,
            provenance_extra={
                "source_system": prov.source_system,
                "content_type": prov.content_type,
                "contained_annotations": bool(prov.contained_annotations),
                "dropped_annotation_count": record.dropped_annotation_count,
                "chunk_count": len(record.chunks),
            },
        )
