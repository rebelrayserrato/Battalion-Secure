"""Tests for the RAYAAAA-274 (270 P2) web-law ingest pipeline.

Covers the counsel/CTO acceptance conditions:
* no-PII structured query contract (Condition C);
* official-host allowlist + https/443 egress guard (CTO 272 / P1 273);
* statutory-only extraction, annotations stripped + flagged (Condition B);
* jurisdiction hard-filter preserved (251 AC-C);
* full provenance on every staged chunk (Condition B/F);
* STAGING only, never the live index, and INERT while flag-off (Condition D / CTO 5).

No network: a stub transport returns synthetic JSON. Staging goes to tmp_path.
"""
from __future__ import annotations

import pytest

from review_engine.config import settings
from review_engine.law.web import (
    Citation,
    FeatureDisabled,
    GovInfoAdapter,
    JurisdictionLeak,
    LawQuery,
    NoPIIViolation,
    WebLawIngestPipeline,
    WebLawProvenance,
    WebLawStagingStore,
    adapter_for,
    extract_statutory,
)
from review_engine.law.web.adapters import EgressBlocked, ProxyHttpTransport, _assert_official_url


# --------------------------------------------------------------------------- #
# Stub transport                                                              #
# --------------------------------------------------------------------------- #
class StubTransport:
    def __init__(self, payload: dict, *, capture: list | None = None):
        self.payload = payload
        self.capture = capture if capture is not None else []

    def get_json(self, url: str) -> dict:
        # Re-assert the allowlist like the real transport, so a bad URL is caught
        # even under the stub.
        _assert_official_url(url)
        self.capture.append(url)
        return dict(self.payload)


GOVINFO_PAYLOAD = {
    "title": "29 U.S.C. § 552",
    "citation": "29 USC 552",
    "dateIssued": "2023-01-01",
    "text": "Section 552. Statutory operative text of the section.\n\n"
    "More operative statutory text follows here.",
}


def _fed_query() -> LawQuery:
    return LawQuery(
        jurisdiction="federal",
        source_system="govinfo",
        citation=Citation(collection="USCODE", title="29", section="552"),
    )


# --------------------------------------------------------------------------- #
# 1. No-PII query contract (Condition C)                                      #
# --------------------------------------------------------------------------- #
def test_query_accepts_structured_citation():
    q = _fed_query().validated()
    assert q.jurisdiction == "federal"
    assert q.source_system == "govinfo"
    assert q.citation.section == "552"


def test_query_rejects_free_text_in_citation():
    # A sentence / PII-shaped value must be refused, not sent on the wire.
    bad = LawQuery(
        jurisdiction="federal",
        source_system="govinfo",
        citation=Citation(section="please find the record for john.doe@example.com asap"),
    )
    with pytest.raises(NoPIIViolation):
        bad.validated()


def test_query_rejects_empty_citation():
    with pytest.raises(NoPIIViolation):
        LawQuery(jurisdiction="federal", source_system="govinfo", citation=Citation()).validated()


def test_query_rejects_unknown_source_system():
    with pytest.raises(NoPIIViolation):
        LawQuery(jurisdiction="federal", source_system="google", citation=Citation(section="1")).validated()


def test_query_rejects_bad_jurisdiction():
    with pytest.raises(ValueError):
        LawQuery(jurisdiction="Atlantis", source_system="ecfr", citation=Citation(title="1")).validated()


# --------------------------------------------------------------------------- #
# 2. Egress allowlist (CTO 272 / P1 273)                                      #
# --------------------------------------------------------------------------- #
def test_egress_guard_allows_official_https_host():
    assert _assert_official_url("https://api.govinfo.gov/packages/x/summary")


@pytest.mark.parametrize(
    "url",
    [
        "http://api.govinfo.gov/x",          # not https
        "https://api.govinfo.gov:8080/x",    # not 443
        "https://evil.example.com/x",        # not allowlisted
        "https://govinfo.gov.evil.com/x",    # lookalike host
    ],
)
def test_egress_guard_blocks_bad_urls(url):
    with pytest.raises(EgressBlocked):
        _assert_official_url(url)


def test_all_adapters_build_allowlisted_urls():
    for system, cite in [
        ("govinfo", Citation(collection="USCODE", title="29", section="552")),
        ("congress", Citation(congress="118", identifier="pub/1")),
        ("ecfr", Citation(title="29", part="1630", section="1630.2")),
    ]:
        q = LawQuery(jurisdiction="federal", source_system=system, citation=cite).validated()
        url = adapter_for(system).build_url(q)
        assert _assert_official_url(url) == url


def test_proxy_transport_fails_closed_without_proxy(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    with pytest.raises(EgressBlocked):
        ProxyHttpTransport().get_json("https://api.govinfo.gov/packages/x/summary")


# --------------------------------------------------------------------------- #
# 3. Statutory-only extraction (Condition B)                                  #
# --------------------------------------------------------------------------- #
def test_extraction_keeps_statutory_drops_annotations():
    raw = (
        "Section 1. The operative statutory text of section one.\n\n"
        "Notes of Decisions\n\n"
        "1. Construction. Some West annotation summarizing a case. Thomson Reuters.\n\n"
        "Section 2. More operative statutory text."
    )
    result = extract_statutory(raw)
    assert "operative statutory text of section one" in result.statutory_text
    assert "More operative statutory text" in result.statutory_text
    assert "West annotation" not in result.statutory_text
    assert result.contained_annotations is True
    assert result.content_type == "statutory"
    assert len(result.dropped_annotations) >= 1


def test_extraction_publisher_marker_block_dropped():
    raw = (
        "Section 5. Pure statutory text.\n\n"
        "Copyright © West Publishing. All rights reserved. Editorial matter."
    )
    result = extract_statutory(raw)
    assert "Pure statutory text" in result.statutory_text
    assert "West Publishing" not in result.statutory_text
    assert result.contained_annotations is True


def test_extraction_pure_statutory_has_no_annotation_flag():
    result = extract_statutory("Section 9. Just the statute, nothing else.")
    assert result.contained_annotations is False
    assert result.statutory_text.startswith("Section 9")


# --------------------------------------------------------------------------- #
# 4. Provenance (Condition B/F)                                               #
# --------------------------------------------------------------------------- #
def test_provenance_requires_official_source():
    prov = WebLawProvenance(
        source_name="x", source_url="https://api.govinfo.gov/y", effective="2023",
        retrieved="2026-07-18", source_system="govinfo", official_source=False,
    )
    with pytest.raises(ValueError):
        prov.validate()


def test_provenance_requires_base_fields():
    prov = WebLawProvenance(
        source_name="", source_url="", effective="", retrieved="",
        source_system="govinfo", official_source=True,
    )
    with pytest.raises(ValueError):
        prov.validate()


def test_provenance_metadata_has_all_web_keys():
    prov = WebLawProvenance(
        source_name="GPO govinfo", source_url="https://api.govinfo.gov/y",
        effective="2023-01-01", retrieved="2026-07-18", source_system="govinfo",
        official_source=True, content_type="statutory", contained_annotations=True,
    ).validate()
    meta = prov.as_metadata()
    for key in (
        "law_source_name", "law_source_url", "law_effective", "law_retrieved",
        "law_source_system", "law_official_source", "law_content_type",
        "law_contained_annotations",
    ):
        assert key in meta
    assert meta["law_official_source"] is True
    assert meta["law_content_type"] == "statutory"


# --------------------------------------------------------------------------- #
# 5. Pipeline: flag guard (INERT — Condition D / CTO 5)                        #
# --------------------------------------------------------------------------- #
def test_pipeline_inert_by_default(tmp_path):
    # Default flag is OFF; the pipeline must refuse to run.
    assert settings.LAW_WEB_INGEST_ENABLED is False
    pipe = WebLawIngestPipeline(
        StubTransport(GOVINFO_PAYLOAD), staging_sink=WebLawStagingStore(tmp_path)
    )
    with pytest.raises(FeatureDisabled):
        pipe.ingest(_fed_query())
    # Nothing staged.
    assert pipe.staging_sink.list_pending() == []


# --------------------------------------------------------------------------- #
# 6. Pipeline: happy path (enabled) → stages, never the live index            #
# --------------------------------------------------------------------------- #
def test_pipeline_stages_fetched_law(tmp_path, monkeypatch):
    store = WebLawStagingStore(tmp_path)
    transport = StubTransport(GOVINFO_PAYLOAD)
    pipe = WebLawIngestPipeline(transport, staging_sink=store, enabled=True)

    result = pipe.ingest(_fed_query())

    assert result.jurisdiction == "federal"
    assert result.source_system == "govinfo"
    assert result.chunk_count >= 1
    assert transport.capture and transport.capture[0].startswith("https://api.govinfo.gov/")

    pending = store.list_pending("federal")
    assert len(pending) == 1
    rec = pending[0]
    assert rec["status"] == "pending"
    assert rec["jurisdiction"] == "federal"
    assert rec["provenance"]["official_source"] is True
    assert rec["provenance"]["content_type"] == "statutory"
    # Every chunk carries jurisdiction as its key + a source_ref (251 shape).
    for ch in rec["chunks"]:
        assert ch["matter_id"] == "federal"
        assert ch["source_ref"].startswith("SRC-")
    # It went to STAGING, never a live law index dir.
    assert (tmp_path / "federal").exists()
    assert not list(tmp_path.glob("law_indexes*"))


def test_pipeline_strips_annotations_before_staging(tmp_path):
    payload = dict(GOVINFO_PAYLOAD)
    payload["text"] = (
        "Section 3. The operative statute text.\n\n"
        "Notes of Decisions\n\n"
        "1. A West headnote that is copyrighted. Thomson Reuters."
    )
    store = WebLawStagingStore(tmp_path)
    pipe = WebLawIngestPipeline(StubTransport(payload), staging_sink=store, enabled=True)
    result = pipe.ingest(_fed_query())
    assert result.contained_annotations is True
    assert result.dropped_annotation_count >= 1
    rec = store.list_pending("federal")[0]
    body = " ".join(ch["text"] for ch in rec["chunks"])
    assert "operative statute text" in body
    assert "West headnote" not in body


# --------------------------------------------------------------------------- #
# 7. Jurisdiction hard-filter (251 AC-C)                                       #
# --------------------------------------------------------------------------- #
def test_pipeline_blocks_cross_jurisdiction_leak(tmp_path):
    # Adapter parses jurisdiction from the query, so force a leak by wrapping it.
    class LeakyAdapter(GovInfoAdapter):
        def parse(self, query, url, payload):
            doc = super().parse(query, url, payload)
            # Simulate a response mis-filed under a different state.
            object.__setattr__(doc, "jurisdiction", "CA")
            return doc

    store = WebLawStagingStore(tmp_path)
    pipe = WebLawIngestPipeline(StubTransport(GOVINFO_PAYLOAD), staging_sink=store, enabled=True)
    monkey_adapter = LeakyAdapter()
    import review_engine.law.web.pipeline as pl
    orig = pl.adapter_for
    pl.adapter_for = lambda system: monkey_adapter
    try:
        with pytest.raises(JurisdictionLeak):
            pipe.ingest(_fed_query())
    finally:
        pl.adapter_for = orig
    assert store.list_pending() == []


# --------------------------------------------------------------------------- #
# 8. Staging store: discard (RAYAAAA-275 'reject')                             #
# --------------------------------------------------------------------------- #
def test_staging_discard(tmp_path):
    store = WebLawStagingStore(tmp_path)
    pipe = WebLawIngestPipeline(StubTransport(GOVINFO_PAYLOAD), staging_sink=store, enabled=True)
    result = pipe.ingest(_fed_query())
    assert len(store.list_pending()) == 1
    assert store.discard(result.staging_id) is True
    assert store.list_pending() == []
