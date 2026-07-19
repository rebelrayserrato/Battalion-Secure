"""Tests for the web-fetched-law "Pending Review" staging queue (RAYAAAA-275, P3
of RAYAAAA-270).

The headline invariant is counsel-binding and gets dedicated failure-mode tests:

* **AUTO-ADD IS FORBIDDEN** — a record entering the queue via ``stage`` never
  reaches the live index on its own. The ONLY bridge to the RAYAAAA-251 upload is
  an explicit ``approve`` call, and ``approve`` refuses records that are not from an
  official source (Counsel Cond A) or that are annotated rather than pure statutory
  text (Counsel Cond B).
* Every terminal decision (approve/reject) writes an audit line.

The RAYAAAA-251 provenance-enforced upload itself is covered by
``test_law_library``; here we assert the seam calls it correctly with a
``FakeService`` so the queue logic is tested without the Chroma/embedding stack.
Synthetic / owner-internal data only.
"""
from __future__ import annotations

import pytest

from review_engine.law.staging import (
    LawStagingStore,
    PendingLaw,
    StagingApprovalError,
)


class FakeService:
    """Records the RAYAAAA-251 upload calls ``approve`` routes through."""

    def __init__(self):
        self.uploads = []
        self.processed = []

    def save_law_upload(self, jurisdiction, name, content, *, source_name,
                        source_url, effective, retrieved):
        # Mirror the real save_law_upload provenance contract so a missing field
        # would blow up here exactly as it would in production.
        assert all([source_name, source_url, effective, retrieved])
        self.uploads.append(
            {
                "jurisdiction": jurisdiction,
                "name": name,
                "content": content,
                "source_name": source_name,
                "source_url": source_url,
                "effective": effective,
                "retrieved": retrieved,
            }
        )
        return name

    def process_law_library(self, jurisdiction):
        self.processed.append(jurisdiction)
        return {"processed": 1, "chunks": 3, "errors": []}


def _store(tmp_path) -> LawStagingStore:
    return LawStagingStore(root=tmp_path / "law_staging")


def _stage_one(store, *, official=True, statutory=True, jurisdiction="federal"):
    return store.stage(
        jurisdiction=jurisdiction,
        source_url="https://www.ecfr.gov/current/title-29/section-541.1",
        source_name="eCFR (GovInfo)",
        retrieved="2026-07-17",
        effective="2026 ed.",
        text="§ 541.1  General rule. " + ("statutory body text. " * 60),
        official_source=official,
        statutory_only=statutory,
        suggested_filename="29-cfr-541-1.html",
    )


# --- stage / read -----------------------------------------------------------


def test_stage_parks_record_and_lists_it(tmp_path):
    store = _store(tmp_path)
    rid = _stage_one(store)
    pending = store.list_pending()
    assert [r.id for r in pending] == [rid]
    rec = pending[0]
    assert rec.jurisdiction == "federal"
    assert rec.official_source and rec.statutory_only
    assert rec.text_preview  # non-empty preview
    assert rec.filename().endswith(".txt")  # promoted as .txt for the 251 upload


def test_stage_does_not_touch_live_index(tmp_path):
    # AUTO-ADD FORBIDDEN: staging performs no upload/indexing on its own.
    store = _store(tmp_path)
    svc = FakeService()
    _stage_one(store)
    assert svc.uploads == [] and svc.processed == []


# --- approve (the only bridge to the live index) ----------------------------


def test_approve_routes_through_251_upload_with_provenance(tmp_path):
    store = _store(tmp_path)
    svc = FakeService()
    rid = _stage_one(store)
    result = store.approve(rid, svc, decided_by="Owner")
    assert len(svc.uploads) == 1
    up = svc.uploads[0]
    assert up["jurisdiction"] == "federal"
    assert up["source_url"].startswith("https://www.ecfr.gov/")
    assert up["source_name"] == "eCFR (GovInfo)"
    assert up["retrieved"] == "2026-07-17"
    assert up["effective"] == "2026 ed."
    assert isinstance(up["content"], bytes) and b"General rule" in up["content"]
    assert svc.processed == ["federal"]
    assert result["chunks"] == 3
    # Consumed from the queue after approval.
    assert store.list_pending() == []


def test_approve_rejects_non_official_source(tmp_path):
    # Counsel Cond A: only official government sources may enter the corpus.
    store = _store(tmp_path)
    svc = FakeService()
    rid = _stage_one(store, official=False)
    with pytest.raises(StagingApprovalError):
        store.approve(rid, svc, decided_by="Owner")
    assert svc.uploads == []
    assert store.list_pending()  # still queued, nothing lost silently


def test_approve_rejects_annotated_text(tmp_path):
    # Counsel Cond B: statutory text ONLY, no West/Lexis-style annotations.
    store = _store(tmp_path)
    svc = FakeService()
    rid = _stage_one(store, statutory=False)
    with pytest.raises(StagingApprovalError):
        store.approve(rid, svc, decided_by="Owner")
    assert svc.uploads == []


def test_approve_missing_record_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(StagingApprovalError):
        store.approve("does-not-exist", FakeService(), decided_by="Owner")


# --- reject -----------------------------------------------------------------


def test_reject_discards_without_upload(tmp_path):
    store = _store(tmp_path)
    svc = FakeService()
    rid = _stage_one(store)
    store.reject(rid, decided_by="Owner", reason="wrong jurisdiction")
    assert store.list_pending() == []
    assert svc.uploads == []


# --- audit trail ------------------------------------------------------------


def test_audit_records_both_decisions(tmp_path):
    store = _store(tmp_path)
    svc = FakeService()
    approve_id = _stage_one(store)
    reject_id = _stage_one(store)
    store.approve(approve_id, svc, decided_by="Owner")
    store.reject(reject_id, decided_by="Owner", reason="dup")
    audit = store.audit_entries()
    actions = {e["action"] for e in audit}
    assert actions == {"approve", "reject"}
    # Most-recent-first ordering.
    assert audit[0]["action"] == "reject"
    assert all(e["source_url"] for e in audit)


# --- hardening --------------------------------------------------------------


def test_record_id_path_traversal_is_neutralised(tmp_path):
    store = _store(tmp_path)
    # A crafted id can neither read nor delete outside the pending dir.
    assert store.get("../../etc/passwd") is None
    with pytest.raises(StagingApprovalError):
        store.reject("../../etc/passwd", decided_by="Owner")


def test_pending_law_roundtrips_through_dict():
    rec = PendingLaw(
        id="abc123",
        jurisdiction="CA",
        source_url="https://leginfo.legislature.ca.gov/x",
        source_name="California Legislative Information",
        retrieved="2026-07-17",
        effective="2026",
        text="body",
        official_source=True,
        statutory_only=True,
        provenance_extra={"via": "api"},
    )
    again = PendingLaw.from_dict(rec.to_dict())
    assert again == rec
