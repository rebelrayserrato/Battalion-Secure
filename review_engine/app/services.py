from __future__ import annotations

import re
import shutil
from pathlib import Path

from review_engine.audits.database import ReviewDatabase
from review_engine.clients.policy_library import PolicyLibraryIndex
from review_engine.compare.redline import ComparisonResult, compare_documents
from review_engine.config.settings import (
    POLICY_UPLOADS_DIR,
    SUPPORTED_EXTENSIONS,
    UPLOADS_DIR,
    ensure_directories,
)
from review_engine.evidence.contradictions import detect_contradictions
from review_engine.evidence.entities import extract_entities
from review_engine.evidence.findings import finalize_findings
from review_engine.evidence.index import EvidenceIndex
from review_engine.evidence.timeline import build_timeline
from review_engine.extraction.extractors import extract_document
from review_engine.fraud_detection.review import run_fraud_review
from review_engine.legal_hr_review.review import run_hr_legal_review


def safe_filename(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip() or "upload"


class ReviewService:
    def __init__(self, database: ReviewDatabase | None = None):
        ensure_directories()
        self.db = database or ReviewDatabase()

    def save_upload(self, matter_id: str, name: str, content: bytes) -> Path:
        clean_name = safe_filename(name)
        extension = Path(clean_name).suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported file type: {extension}")
        target_dir = UPLOADS_DIR / matter_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / clean_name
        target.write_bytes(content)
        self.db.add_document(matter_id, clean_name, target)
        return target

    def import_file(self, matter_id: str, source: str | Path) -> Path:
        source = Path(source)
        return self.save_upload(matter_id, source.name, source.read_bytes())

    def process_matter(self, matter_id: str) -> dict:
        processed = 0
        errors = []
        for document in self.db.list_documents(matter_id):
            try:
                chunks = extract_document(document["path"], matter_id)
                self.db.replace_document_chunks(matter_id, document["name"], chunks)
                processed += 1
            except Exception as exc:
                message = f"{document['name']}: {exc}"
                errors.append(message)
                self.db.log("error", matter_id, message)
        chunks = self.db.get_chunks(matter_id)
        self.db.replace_entities(matter_id, extract_entities(chunks))
        try:
            count = EvidenceIndex(matter_id).build(chunks)
            self.db.log("index_created", matter_id, f"{count} chunks")
        except Exception as exc:
            errors.append(f"Index: {exc}")
            self.db.log("error", matter_id, f"Index: {exc}")
        return {"processed": processed, "chunks": len(chunks), "errors": errors}

    def run_reviews(self, matter_id: str, include_hr: bool = True, include_fraud: bool = True) -> list[dict]:
        chunks = self.db.get_chunks(matter_id)
        matter = self.db.get_matter(matter_id) or {}
        candidates = []
        if include_hr:
            candidates.extend(run_hr_legal_review(chunks, matter.get("jurisdiction", "")))
        if include_fraud:
            paths = [doc["path"] for doc in self.db.list_documents(matter_id)]
            candidates.extend(run_fraud_review(paths, chunks))
        candidates.extend(detect_contradictions(chunks))
        findings = finalize_findings(candidates)
        self.db.replace_findings(matter_id, findings)
        self.db.log(
            "review_run",
            matter_id,
            f"{len(findings)} source-supported findings; HR={include_hr}; fraud={include_fraud}",
        )
        return findings

    def timeline(self, matter_id: str) -> list[dict]:
        return build_timeline(self.db.get_chunks(matter_id))

    def search(self, matter_id: str, query: str, limit: int = 8) -> list[dict]:
        return EvidenceIndex(matter_id).search(query, limit)

    # --- Client policy library (RAYAAAA-245, Phase B) ----------------------
    #
    # Uploading, ingesting, and searching a Client's own HR/company policy
    # corpus. This reuses the exact extraction pipeline (extract_document, incl.
    # the RAYAAAA-230 OCR/image/ZIP handling) and the EvidenceIndex machinery —
    # there is no parallel ingestion system. Storage is client-scoped and lives
    # apart from every Task workspace.

    def save_policy_upload(self, client_id: str, name: str, content: bytes) -> Path:
        clean_name = safe_filename(name)
        extension = Path(clean_name).suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported file type: {extension}")
        target_dir = POLICY_UPLOADS_DIR / client_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / clean_name
        target.write_bytes(content)
        self.db.add_policy_document(client_id, clean_name, target)
        return target

    def process_policy_library(self, client_id: str) -> dict:
        """Extract + (re)index a Client's uploaded policy documents.

        Chunks are salted by ``client_id`` (passed as the extractor's matter_id),
        stored in ``policy_chunks``, then written to the client-scoped
        ``PolicyLibraryIndex`` — a physically separate Chroma store keyed by the
        client id, so it can never be reached from another client's Task query.
        """
        processed = 0
        errors: list[str] = []
        for document in self.db.list_policy_documents(client_id):
            try:
                chunks = extract_document(document["path"], client_id)
                self.db.replace_policy_document_chunks(client_id, document["name"], chunks)
                processed += 1
            except Exception as exc:
                message = f"{document['name']}: {exc}"
                errors.append(message)
                self.db.log("error", None, f"policy {client_id}: {message}")
        chunks = self.db.get_policy_chunks(client_id)
        try:
            count = PolicyLibraryIndex(client_id).build(chunks)
            self.db.log("policy_index_created", None, f"{client_id}: {count} chunks")
        except Exception as exc:
            errors.append(f"Index: {exc}")
            self.db.log("error", None, f"policy index {client_id}: {exc}")
        return {"processed": processed, "chunks": len(chunks), "errors": errors}

    def policy_search(self, client_id: str, query: str, limit: int = 8) -> list[dict]:
        return PolicyLibraryIndex(client_id).search(query, limit)

    def delete_policy_document(self, client_id: str, document_name: str) -> dict:
        """Remove one policy document and rebuild the client's policy index."""
        self.db.delete_policy_document(client_id, document_name)
        remaining = self.db.get_policy_chunks(client_id)
        count = PolicyLibraryIndex(client_id).build(remaining)
        return {"remaining_documents": len(self.db.list_policy_documents(client_id)), "chunks": count}

    def document_chunks(self, matter_id: str, document_name: str) -> list:
        """Processed chunks for a single document, in reading order.

        Reuses the existing chunk store (RAYAAAA-231): ``get_chunks`` already
        orders by document, page, then row, so this preserves document order.
        """
        return [
            chunk
            for chunk in self.db.get_chunks(matter_id)
            if chunk.document_name == document_name
        ]

    def compare_documents(
        self,
        matter_id: str,
        base_name: str,
        compare_name: str,
        *,
        include_unchanged: bool = False,
    ) -> ComparisonResult:
        """Deterministic redline between two processed documents in a Task."""
        result = compare_documents(
            base_name,
            self.document_chunks(matter_id, base_name),
            compare_name,
            self.document_chunks(matter_id, compare_name),
            include_unchanged=include_unchanged,
        )
        self.db.log(
            "document_compare",
            matter_id,
            f"{base_name} vs {compare_name}: {result.counts}",
        )
        return result
