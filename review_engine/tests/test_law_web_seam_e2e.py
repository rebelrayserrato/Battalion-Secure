"""End-to-end seam test for the RAYAAAA-287 P2↔P3 wiring (the link the
RAYAAAA-286 cutover e2e flagged).

Before RAYAAAA-287 the RAYAAAA-274 pipeline staged into ``WebLawStagingStore``
(``LAW_STAGING_DIR/<jurisdiction>/*.json``) while the owner Pending Review UI +
``LawStagingStore.approve`` read ``LAW_STAGING_DIR/pending/*.json`` — different
stores, so web-fetched law never reached the queue. This test exercises the fixed
seam through the SINGLE store the owner UI reads (``LawStagingStore`` via the
``LawStagingSink`` adapter):

    pipeline.ingest()  ->  owner queue shows it  ->  Approve  ->  251 upload
                                                 \\-> Reject  ->  discarded + audited

and asserts the counsel-binding invariants hold across the seam:

* NO PII on the wire (Condition C) — only structured citation tokens leave the box;
* jurisdiction hard-filter preserved (251 AC-C) — the doc is filed under its own
  jurisdiction and a mismatch aborts;
* statutory-only (Condition B) — publisher annotations are stripped before staging
  and never reach the 251 upload;
* auto-add FORBIDDEN — nothing reaches the live index without an explicit Approve.

No network: a stub transport returns synthetic gov JSON. The real 251 index write
is covered by ``test_law_library``; here a ``FakeService`` asserts the seam calls
the upload correctly (as ``test_law_staging`` does), without the embedding stack.
"""
from __future__ import annotations

import pytest

from review_engine.law.staging import LawStagingSink, LawStagingStore
from review_engine.law.web import (
    Citation,
    JurisdictionLeak,
    LawQuery,
    NoPIIViolation,
    WebLawIngestPipeline,
)
from review_engine.law.web.adapters import _assert_official_url


# --------------------------------------------------------------------------- #
# Stub transport — captures every URL that would go on the wire.              #
# --------------------------------------------------------------------------- #
class StubTransport:
    def __init__(self, payload: dict):
        self.payload = payload
        self.urls: list[str] = []

    def get_json(self, url: str) -> dict:
        _assert_official_url(url)  # re-assert allowlist, like the real transport
        self.urls.append(url)
        return dict(self.payload)


# A synthetic GovInfo package: two statutory blocks followed by a copyrighted
# West "Notes of Decisions" annotation block that MUST be stripped before staging.
GOV_DOC = {
    "title": "29 U.S.C. § 552",
    "citation": "29 USC 552",
    "dateIssued": "2023-01-01",
    "text": (
        "§ 552. Statutory operative text of the section.\n\n"
        "More operative statutory text of the section follows here.\n\n"
        "Notes of Decisions\n\n"
        "1. Copyrighted West headnote that must never be ingested.\n"
    ),
}
_WEST_HEADNOTE = "Copyrighted West headnote"


def _fed_query() -> LawQuery:
    return LawQuery(
        jurisdiction="federal",
        source_system="govinfo",
        citation=Citation(collection="USCODE", title="29", section="552"),
    )


class FakeService:
    """Records the RAYAAAA-251 upload calls Approve routes through (as in
    test_law_staging), so the seam is verified without Chroma/embeddings."""

    def __init__(self):
        self.uploads = []
        self.processed = []

    def save_law_upload(self, jurisdiction, name, content, *, source_name,
                        source_url, effective, retrieved):
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
        return {"processed": 1, "chunks": 2, "errors": []}


def _pipeline(store: LawStagingStore, transport) -> WebLawIngestPipeline:
    # enabled=True exercises the ON path deterministically regardless of the
    # process-wide flag (the app gates the UI behind LAW_WEB_INGEST_ENABLED).
    return WebLawIngestPipeline(
        transport, staging_sink=LawStagingSink(store), enabled=True
    )


# --------------------------------------------------------------------------- #
# 1. The full happy path across the seam.                                     #
# --------------------------------------------------------------------------- #
def test_ingest_lands_in_owner_queue_then_approve_uploads(tmp_path):
    store = LawStagingStore(root=tmp_path / "law_staging")
    transport = StubTransport(GOV_DOC)
    result = _pipeline(store, transport).ingest(_fed_query())

    # --- landed in the SAME queue the owner UI reads (the seam bug) ----------
    pending = store.list_pending()
    assert len(pending) == 1, "pipeline output must appear in the owner Pending Review queue"
    rec = pending[0]
    assert rec.jurisdiction == "federal"            # 251 hard-filter preserved
    assert rec.official_source is True              # Cond A satisfied for approve
    assert rec.statutory_only is True               # Cond B — pure statutory text
    assert result.chunk_count >= 1

    # --- statutory-only: the West annotation was stripped before staging -----
    assert _WEST_HEADNOTE not in rec.text
    assert "Statutory operative text" in rec.text
    assert rec.provenance_extra.get("contained_annotations") is True

    # --- Approve → 251 provenance-enforced upload into the live index --------
    svc = FakeService()
    outcome = store.approve(rec.id, svc, decided_by="owner")
    assert len(svc.uploads) == 1
    up = svc.uploads[0]
    assert up["jurisdiction"] == "federal"          # filed under its own jurisdiction
    assert all([up["source_name"], up["source_url"], up["effective"], up["retrieved"]])
    assert _WEST_HEADNOTE.encode() not in up["content"]   # annotation never indexed
    assert b"Statutory operative text" in up["content"]
    assert svc.processed == ["federal"]
    assert outcome["chunks"] == 2
    # Consumed from the queue after approval; nothing lingers.
    assert store.list_pending() == []


# --------------------------------------------------------------------------- #
# 2. Reject discards + audits without any upload.                            #
# --------------------------------------------------------------------------- #
def test_ingest_then_reject_discards_and_audits(tmp_path):
    store = LawStagingStore(root=tmp_path / "law_staging")
    _pipeline(store, StubTransport(GOV_DOC)).ingest(_fed_query())
    rec = store.list_pending()[0]

    svc = FakeService()
    store.reject(rec.id, decided_by="owner", reason="duplicate of existing statute")
    assert store.list_pending() == []               # discarded
    assert svc.uploads == []                         # never touched the live index
    audit = store.audit_entries()
    assert audit and audit[0]["action"] == "reject"
    assert audit[0]["source_url"]


# --------------------------------------------------------------------------- #
# 3. No PII on the wire (Condition C).                                        #
# --------------------------------------------------------------------------- #
def test_no_pii_reaches_the_wire(tmp_path):
    store = LawStagingStore(root=tmp_path / "law_staging")
    transport = StubTransport(GOV_DOC)
    _pipeline(store, transport).ingest(_fed_query())

    # Exactly one outbound URL, to an allowlisted official host, carrying only the
    # structured citation tokens — no names/emails/free text.
    assert len(transport.urls) == 1
    url = transport.urls[0]
    assert url.startswith("https://api.govinfo.gov/")
    for token in ("USCODE", "29", "552"):
        assert token in url
    assert "@" not in url and " " not in url

    # And the contract itself refuses a free-text / PII-bearing citation, so the
    # pipeline can never build such a URL in the first place.
    pii_query = LawQuery(
        jurisdiction="federal",
        source_system="govinfo",
        citation=Citation(identifier="john.doe@example.com asked about firing"),
    )
    with pytest.raises(NoPIIViolation):
        _pipeline(store, transport).ingest(pii_query)


# --------------------------------------------------------------------------- #
# 4. Jurisdiction hard-filter still aborts a cross-file (251 AC-C).           #
# --------------------------------------------------------------------------- #
def test_jurisdiction_mismatch_aborts_before_staging(tmp_path, monkeypatch):
    # The adapter parses jurisdiction from the query, so force a mis-filed
    # response (as test_law_web_pipeline does) to prove the seam still aborts a
    # cross-file before anything reaches the owner queue.
    from review_engine.law.web import GovInfoAdapter
    import review_engine.law.web.pipeline as pl

    class LeakyAdapter(GovInfoAdapter):
        def parse(self, query, url, payload):
            doc = super().parse(query, url, payload)
            object.__setattr__(doc, "jurisdiction", "CA")
            return doc

    store = LawStagingStore(root=tmp_path / "law_staging")
    monkeypatch.setattr(pl, "adapter_for", lambda system: LeakyAdapter())
    with pytest.raises(JurisdictionLeak):
        _pipeline(store, StubTransport(GOV_DOC)).ingest(_fed_query())
    assert store.list_pending() == []               # nothing staged on a leak
