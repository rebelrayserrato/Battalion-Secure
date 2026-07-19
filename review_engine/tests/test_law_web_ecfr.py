"""eCFR live-adapter + credential-wiring tests (RAYAAAA-289).

RAYAAAA-274 built the three official-source adapters but only ever exercised them
against MOCKED JSON. RAYAAAA-289 makes them work against the LIVE gov APIs:

* **eCFR** ``/versioner/v1/full/`` is **XML-only** (the ``.json`` variant returns
  HTTP 406, confirmed live 2026-07-19). :class:`ECFRAdapter` now fetches XML and
  parses the ``DIV*`` → ``HEAD``/``P`` section structure into statutory text.
* **govinfo / congress** need the env-only ``api.data.gov`` key, injected by the
  transport at send time; absent a key they fail closed (:class:`MissingCredential`).

The bulk of this file is HERMETIC (no network): the eCFR XML parser and the
credential logic are tested with a captured XML sample and a stub transport, so
CI stays offline. One LIVE integration test at the bottom is gated behind
``RAYAAAA_289_LIVE=1`` (and needs ``HTTPS_PROXY`` to the RAYAAAA-273 egress proxy)
so it never runs in the default hermetic suite. All the RAYAAAA-274 invariants
(no-PII query, jurisdiction hard-filter, statutory-only, staging-only) are
asserted on the real fetch.
"""
from __future__ import annotations

import os

import pytest

from review_engine.law.library import FEDERAL_JURISDICTION
from review_engine.law.web.adapters import (
    ECFRAdapter,
    MissingCredential,
    ProxyHttpTransport,
    _assert_official_url,
    _default_ecfr_date,
)
from review_engine.law.web.pipeline import (
    JurisdictionLeak,
    WebLawIngestPipeline,
)
from review_engine.law.web.query import Citation, LawQuery
from review_engine.law.web.staging import WebLawStagingStore


# A trimmed but structurally faithful eCFR ``/full/`` XML body: a DIV8 SECTION
# with a HEAD, operative P paragraphs, an editorial NOTE (must be dropped), and a
# CITA source line (must be dropped). Matches the shape of the live response for
# 29 CFR 1630.2 (verified live 2026-07-19).
ECFR_XML_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<DIV8 N="1630.2" TYPE="SECTION" hierarchy_metadata='{"citation":"29 CFR 1630.2"}'>
  <HEAD>&#167; 1630.2 Definitions.</HEAD>
  <P>(a) <I>Commission</I> means the Equal Employment Opportunity Commission.</P>
  <P>(b) <I>Covered entity</I> means an employer, employment agency, or labor organization.</P>
  <NOTE>
    <HED>Editorial Note:</HED>
    <P>THIS_IS_EDITORIAL_APPARATUS that must never be ingested.</P>
  </NOTE>
  <CITA>[56 FR 35734, July 26, 1991]</CITA>
</DIV8>
"""


# eCFR's titles.json (trimmed) — the fetch path reads this to resolve the latest
# available date for a title before requesting /full/{date}.
ECFR_TITLES = {
    "titles": [
        {"number": 29, "name": "Labor", "up_to_date_as_of": "2026-07-16",
         "latest_issue_date": "2026-07-16"},
    ]
}


class StubTextTransport:
    """Stub transport for the eCFR path.

    ``get_text`` returns the XML body (captured for assertions); ``get_json``
    serves the ``titles.json`` used to resolve the latest date. Re-asserts the
    allowlist like the real transport so a bad URL is caught even under the stub.
    """

    def __init__(self, body: str, *, titles: dict | None = None, capture: list | None = None):
        self.body = body
        self.titles = titles if titles is not None else ECFR_TITLES
        self.capture = capture if capture is not None else []

    def get_text(self, url: str) -> str:
        _assert_official_url(url)
        self.capture.append(url)
        return self.body

    def get_json(self, url: str) -> dict:
        _assert_official_url(url)
        assert url.endswith("/titles.json"), f"unexpected JSON fetch: {url}"
        return dict(self.titles)


def _ecfr_query(jurisdiction: str = FEDERAL_JURISDICTION) -> LawQuery:
    return LawQuery(
        jurisdiction=jurisdiction,
        source_system="ecfr",
        citation=Citation(title="29", part="1630", section="1630.2"),
    )


# --------------------------------------------------------------------------- #
# 1. eCFR builds the XML URL (not .json → which the live API 406s)             #
# --------------------------------------------------------------------------- #
def test_ecfr_url_is_xml_with_concrete_date():
    url = ECFRAdapter().build_url(_ecfr_query().validated())
    assert _assert_official_url(url) == url
    assert url.startswith("https://api.ecfr.gov/api/versioner/v1/full/")
    assert ".xml" in url and ".json" not in url
    # A concrete YYYY-MM-DD date, never the literal "current" (invalid on /full/).
    assert "/current/" not in url
    assert f"/full/{_default_ecfr_date()}/title-29.xml" in url
    assert "part=1630" in url and "section=1630.2" in url


def test_ecfr_url_honours_explicit_version_date():
    q = LawQuery(
        jurisdiction=FEDERAL_JURISDICTION,
        source_system="ecfr",
        citation=Citation(title="29", section="552", version_date="2023-01-01"),
    ).validated()
    url = ECFRAdapter().build_url(q)
    assert "/full/2023-01-01/title-29.xml" in url


# --------------------------------------------------------------------------- #
# 2. eCFR XML parsing → statutory text, drops editorial apparatus             #
# --------------------------------------------------------------------------- #
def test_ecfr_parses_xml_to_statutory_text():
    adapter = ECFRAdapter()
    q = _ecfr_query().validated()
    url = adapter.build_url(q)
    raw = adapter.parse_xml(q, url, ECFR_XML_SAMPLE)

    assert "Equal Employment Opportunity Commission" in raw.text
    assert "Covered entity" in raw.text
    # Editorial NOTE + CITA source line are pruned at the XML boundary.
    assert "THIS_IS_EDITORIAL_APPARATUS" not in raw.text
    assert "56 FR 35734" not in raw.text
    # eCFR is a FEDERAL source; asserting so lets the pipeline hard-filter reject
    # a mis-targeted non-federal query (251 AC-C).
    assert raw.jurisdiction == FEDERAL_JURISDICTION
    assert raw.official_source is True
    assert raw.citation == "29 CFR 1630.2"  # from hierarchy_metadata
    assert "1630.2" in raw.title


# --------------------------------------------------------------------------- #
# 3. Full pipeline over the XML adapter → stages, never the live index         #
# --------------------------------------------------------------------------- #
def test_pipeline_stages_ecfr_xml(tmp_path):
    store = WebLawStagingStore(tmp_path)
    transport = StubTextTransport(ECFR_XML_SAMPLE)
    pipe = WebLawIngestPipeline(transport, staging_sink=store, enabled=True)

    result = pipe.ingest(_ecfr_query())

    assert result.source_system == "ecfr"
    assert result.jurisdiction == FEDERAL_JURISDICTION
    assert result.chunk_count >= 1
    assert transport.capture and ".xml" in transport.capture[0]

    pending = store.list_pending(FEDERAL_JURISDICTION)
    assert len(pending) == 1
    rec = pending[0]
    assert rec["provenance"]["source_system"] == "ecfr"
    assert rec["provenance"]["official_source"] is True
    assert rec["provenance"]["content_type"] == "statutory"
    body = " ".join(ch["text"] for ch in rec["chunks"])
    assert "Equal Employment Opportunity Commission" in body
    assert "THIS_IS_EDITORIAL_APPARATUS" not in body
    # Staged only — no live index dir was created.
    assert (tmp_path / FEDERAL_JURISDICTION).exists()
    assert not list(tmp_path.glob("law_indexes*"))


def test_ecfr_fetch_resolves_latest_available_date():
    # With no pinned version_date, fetch() asks eCFR for the title's latest date
    # (via titles.json) and requests /full/{that date}/ — never a guessed "today"
    # that the live API would 404.
    adapter = ECFRAdapter()
    transport = StubTextTransport(ECFR_XML_SAMPLE)
    raw = adapter.fetch(_ecfr_query(), transport)
    assert raw.effective == "2026-07-16"
    assert transport.capture and "/full/2026-07-16/title-29.xml" in transport.capture[0]
    assert "/full/2026-07-16/" in raw.source_url


def test_pipeline_rejects_non_federal_ecfr_query(tmp_path):
    # eCFR is federal-only; a state-targeted eCFR query must be caught by the
    # jurisdiction hard-filter and never staged.
    store = WebLawStagingStore(tmp_path)
    pipe = WebLawIngestPipeline(
        StubTextTransport(ECFR_XML_SAMPLE), staging_sink=store, enabled=True
    )
    with pytest.raises(JurisdictionLeak):
        pipe.ingest(_ecfr_query(jurisdiction="CA"))
    assert store.list_pending() == []


# --------------------------------------------------------------------------- #
# 4. api.data.gov credential wiring (env-only, fail-closed) — Counsel C-6      #
# --------------------------------------------------------------------------- #
def test_ecfr_needs_no_api_key():
    t = ProxyHttpTransport(api_keys={})
    url = "https://api.ecfr.gov/api/versioner/v1/full/2024-01-01/title-29.xml"
    assert t._with_api_key(url) == url  # keyless — unchanged


def test_keyed_hosts_fail_closed_without_key():
    t = ProxyHttpTransport(api_keys={})
    for host in ("api.govinfo.gov", "api.congress.gov"):
        with pytest.raises(MissingCredential):
            t._with_api_key(f"https://{host}/v3/something")


def test_keyed_host_appends_env_key():
    t = ProxyHttpTransport(api_keys={"api.govinfo.gov": "SECRET-KEY"})
    out = t._with_api_key("https://api.govinfo.gov/packages/x/summary")
    assert out == "https://api.govinfo.gov/packages/x/summary?api_key=SECRET-KEY"
    # An existing query string uses & rather than ?.
    out2 = t._with_api_key("https://api.govinfo.gov/x?foo=bar")
    assert out2.endswith("?foo=bar&api_key=SECRET-KEY")


def test_transport_loads_keys_from_env(monkeypatch):
    monkeypatch.setenv("DATA_GOV_API_KEY", "ENV-KEY")
    monkeypatch.delenv("CONGRESS_GOV_API_KEY", raising=False)
    t = ProxyHttpTransport()  # api_keys=None → load from env
    assert t._with_api_key("https://api.govinfo.gov/x").endswith("api_key=ENV-KEY")
    # The shared api.data.gov key also serves congress unless overridden.
    assert t._with_api_key("https://api.congress.gov/x").endswith("api_key=ENV-KEY")


def test_missing_credential_is_egress_blocked():
    # Callers that already fail-close on egress problems must treat a missing
    # credential the same way (it subclasses EgressBlocked).
    from review_engine.law.web.adapters import EgressBlocked

    assert issubclass(MissingCredential, EgressBlocked)


# --------------------------------------------------------------------------- #
# 5. LIVE integration (network) — env-gated so CI stays hermetic               #
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not os.getenv("RAYAAAA_289_LIVE"),
    reason="live eCFR fetch — set RAYAAAA_289_LIVE=1 (needs HTTPS_PROXY egress proxy)",
)
def test_live_ecfr_fetch_extract_stage(tmp_path):
    """Real eCFR fetch → extract → stage, asserting the RAYAAAA-274 invariants.

    Runs only when RAYAAAA_289_LIVE=1 and an HTTPS_PROXY (RAYAAAA-273 egress
    proxy) is configured, i.e. from inside the review-engine container. Proves
    the adapter fetches non-empty statutory text from the live API and that
    no-PII query + jurisdiction hard-filter + statutory-only still hold.
    """
    store = WebLawStagingStore(tmp_path)
    pipe = WebLawIngestPipeline(
        ProxyHttpTransport(), staging_sink=store, enabled=True
    )
    result = pipe.ingest(_ecfr_query())

    assert result.source_system == "ecfr"
    assert result.jurisdiction == FEDERAL_JURISDICTION
    assert result.chunk_count >= 1

    rec = store.list_pending(FEDERAL_JURISDICTION)[0]
    body = " ".join(ch["text"] for ch in rec["chunks"])
    assert body.strip(), "live eCFR fetch produced empty statutory text"
    # 29 CFR 1630.2 is the ADA definitions section — a stable anchor word.
    assert "means" in body.lower()
    assert rec["provenance"]["official_source"] is True
    assert rec["provenance"]["content_type"] == "statutory"
    assert rec["provenance"]["source_url"].startswith("https://api.ecfr.gov/")
