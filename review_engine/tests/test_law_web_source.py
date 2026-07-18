"""No-PII outbound law-query boundary tests (RAYAAAA-273, P1 of RAYAAAA-270).

These prove Counsel Condition C (RAYAAAA-271) / CTO Condition 3 (RAYAAAA-272)
STRUCTURALLY: matter text, client identifiers and uploaded-document content
cannot be attached to an outbound law lookup, and only a structured
{jurisdiction, citation, topic} query against an OFFICIAL government publisher
can be built. No network is opened — the request object is only *built*.

Synthetic data only.
"""
from __future__ import annotations

import pytest

from review_engine.law.web_source import (
    ALLOWED_QUERY_FIELDS,
    ALLOWED_TOPICS,
    LawWebQuery,
    OutboundRequest,
    assert_no_pii_leak,
    build_outbound_request,
    get_official_source,
    is_law_web_ingest_enabled,
    official_sources,
)


# --- Ships INERT -------------------------------------------------------------
def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("LAW_WEB_INGEST_ENABLED", raising=False)
    assert is_law_web_ingest_enabled() is False


# --- The query cannot carry PII / matter content (structural) ----------------
def test_query_has_no_field_for_matter_or_client_data():
    # Exactly the three allowlisted structured fields; nothing for free text.
    assert ALLOWED_QUERY_FIELDS == {"jurisdiction", "citation", "topic"}
    assert set(LawWebQuery.__dataclass_fields__) == ALLOWED_QUERY_FIELDS


def test_unknown_field_cannot_be_attached():
    # A frozen dataclass rejects an unknown constructor keyword, so matter text /
    # client id / document content can never be smuggled onto the query.
    with pytest.raises(TypeError):
        LawWebQuery(  # type: ignore[call-arg]
            jurisdiction="federal",
            citation="42 U.S.C. 1983",
            matter_text="Acme v. Roe — client John Doe, SSN 123-45-6789",
        )


def test_query_is_frozen():
    q = LawWebQuery(jurisdiction="federal", topic="employment")
    with pytest.raises(Exception):
        q.citation = "smuggled free text about the client's matter"  # type: ignore[misc]


@pytest.mark.parametrize(
    "bad_citation",
    [
        "Please research whether our client John Doe can sue Acme for wrongful "
        "termination after the incident on 2026-01-05",  # a sentence / matter text
        "client: John Doe; ssn 123-45-6789",             # PII with disallowed chars
        "email me at john.doe@example.com",              # contains '@' / free text
        "a" * 100,                                        # over length cap
    ],
)
def test_free_text_citation_is_rejected(bad_citation):
    with pytest.raises(ValueError):
        LawWebQuery(jurisdiction="federal", citation=bad_citation)


def test_topic_must_be_controlled_vocabulary():
    # A person's name / free-text phrase is not a valid topic.
    with pytest.raises(ValueError):
        LawWebQuery(jurisdiction="federal", topic="John Doe wrongful termination")
    # A real controlled topic is accepted and normalised.
    assert LawWebQuery(jurisdiction="federal", topic="Employment").topic == "employment"
    assert "employment" in ALLOWED_TOPICS


def test_citation_or_topic_required():
    with pytest.raises(ValueError):
        LawWebQuery(jurisdiction="federal")


def test_structured_citation_is_accepted():
    q = LawWebQuery(jurisdiction="federal", citation="42 U.S.C. § 1983")
    assert q.citation == "42 U.S.C. § 1983"
    assert q.jurisdiction == "federal"


# --- The built outbound request contains only structured fields --------------
def test_built_request_carries_only_allowlisted_params(monkeypatch):
    monkeypatch.setenv("CONGRESS_GOV_API_KEY", "TEST-KEY-not-a-real-secret")
    q = LawWebQuery(jurisdiction="federal", citation="42 U.S.C. 1983", topic="civil rights")
    req = build_outbound_request(q, "congress")

    assert isinstance(req, OutboundRequest)
    assert req.method == "GET"
    assert req.url.startswith("https://api.congress.gov")

    # Only structured keys (+ the api_key) may appear as params.
    allowed_param_keys = {"citation", "query", "jurisdiction", "api_key"}
    assert set(req.params) <= allowed_param_keys


def test_matter_pii_never_appears_in_wire_request(monkeypatch):
    monkeypatch.setenv("CONGRESS_GOV_API_KEY", "TEST-KEY-not-a-real-secret")
    q = LawWebQuery(jurisdiction="federal", citation="42 U.S.C. 1983", topic="employment")
    req = build_outbound_request(q, "congress")

    wire = req.wire_text().lower()
    for pii in ("john doe", "123-45-6789", "acme v. roe", "wrongful termination narrative"):
        assert pii not in wire


def test_assert_no_pii_leak_is_fail_closed(monkeypatch):
    monkeypatch.setenv("CONGRESS_GOV_API_KEY", "TEST-KEY-not-a-real-secret")
    q = LawWebQuery(jurisdiction="federal", topic="employment")
    req = build_outbound_request(q, "congress")

    # A clean request passes the belt-and-suspenders guard.
    assert_no_pii_leak(req, ["John Doe", "123-45-6789", "Acme Corp"])

    # If somehow a PII value were present, the guard must raise.
    tainted = OutboundRequest(
        method="GET",
        url="https://api.congress.gov/v3/law",
        params={"query": "john doe ssn 123-45-6789"},
        headers={},
    )
    with pytest.raises(RuntimeError):
        assert_no_pii_leak(tainted, ["John Doe"])


# --- Only official publishers; env-only keys ---------------------------------
def test_unregistered_host_is_rejected():
    q = LawWebQuery(jurisdiction="federal", topic="tax")
    with pytest.raises(ValueError):
        build_outbound_request(q, "westlaw")  # a paid vendor is not registered


def test_all_registered_sources_are_gov_hosts():
    for src in official_sources().values():
        assert src.host.endswith(".gov"), src.host
        # No wildcard / bare-domain entries — exact hostnames only (CTO Condition 1).
        assert not src.host.startswith("*")
        assert src.host.count(".") >= 2


def test_api_key_is_env_only_and_fail_closed(monkeypatch):
    monkeypatch.delenv("CONGRESS_GOV_API_KEY", raising=False)
    q = LawWebQuery(jurisdiction="federal", topic="tax")
    # No key in env -> refuse to build (fail closed), never send unauthenticated.
    with pytest.raises(RuntimeError):
        build_outbound_request(q, "congress")


def test_keyless_source_builds_without_env(monkeypatch):
    monkeypatch.delenv("DATA_GOV_API_KEY", raising=False)
    q = LawWebQuery(jurisdiction="federal", topic="environmental")
    req = build_outbound_request(q, "ecfr")  # eCFR requires no key
    assert req.url.startswith("https://api.ecfr.gov")
    assert "api_key" not in req.params


def test_source_serves_only_its_jurisdictions():
    src = get_official_source("congress")
    assert src.serves("federal")
    assert not src.serves("CA")  # state hosts are added later (CTO Condition 7)
