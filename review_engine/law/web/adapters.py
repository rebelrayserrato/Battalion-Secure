"""Official-source adapters + the proxy-bound HTTP transport (RAYAAAA-274 P2).

Three adapters, one per official structured API:

* :class:`GovInfoAdapter`   — ``api.govinfo.gov`` (GPO: U.S. Code, CFR, Public Laws)
* :class:`CongressAdapter`  — ``api.congress.gov`` (LoC: bills, public laws)
* :class:`ECFRAdapter`      — ``api.ecfr.gov`` (electronic CFR)

Each adapter turns a validated :class:`~review_engine.law.web.query.LawQuery`
into an exact-host https URL, fetches it through an injected
:class:`HttpTransport`, and parses the structured response into a
:class:`RawLawDocument` (law text + title / citation / jurisdiction / effective
version). No adapter ever builds a URL for a host outside
``LAW_OFFICIAL_SOURCE_HOSTS``; the transport enforces the same allowlist again at
send time (defense in depth with the RAYAAAA-273 egress proxy).

The live transport (:class:`ProxyHttpTransport`) routes exclusively through the
``HTTPS_PROXY`` egress proxy over https/443. Tests inject a stub transport so the
suite never touches the network — and, because the pipeline is flag-off, the live
transport is never even constructed in production yet.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from review_engine.config.settings import LAW_OFFICIAL_SOURCE_HOSTS
from review_engine.law.library import FEDERAL_JURISDICTION
from review_engine.law.web.query import Citation, LawQuery


class EgressBlocked(RuntimeError):
    """Raised when an outbound URL is not an allowlisted official host over https."""


def _assert_official_url(url: str) -> str:
    """Fail-closed check that ``url`` targets an exact allowlisted host on https/443."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise EgressBlocked(f"refusing non-https egress: {url!r}")
    if parts.port not in (None, 443):
        raise EgressBlocked(f"refusing non-443 egress: {url!r}")
    if parts.hostname not in LAW_OFFICIAL_SOURCE_HOSTS:
        raise EgressBlocked(
            f"host {parts.hostname!r} is not an allowlisted official source "
            f"(allowed: {LAW_OFFICIAL_SOURCE_HOSTS})"
        )
    return url


@runtime_checkable
class HttpTransport(Protocol):
    """Fetches a JSON document from an allowlisted official host.

    Implementations MUST route through the RAYAAAA-273 egress proxy and MUST
    re-assert the host allowlist; the query builder guarantees no PII is in the
    URL, and the transport guarantees the URL only reaches an official host."""

    def get_json(self, url: str) -> dict: ...


class ProxyHttpTransport:
    """Live transport: https-only, through ``HTTPS_PROXY``, allowlist-enforced.

    Not used while the feature is flag-off (the pipeline never constructs it).
    When enabled it requires an egress proxy to be configured — there is no
    direct-egress fallback, so if the RAYAAAA-273 proxy is absent it fails closed.
    """

    def __init__(self, *, timeout: float = 15.0, api_keys: dict | None = None):
        self.timeout = timeout
        # API keys (govinfo/congress) come from the environment only, never source.
        self._api_keys = api_keys or {}

    def _proxy(self) -> str:
        proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
        if not proxy:
            raise EgressBlocked(
                "HTTPS_PROXY is not set — refusing to egress without the "
                "RAYAAAA-273 default-deny proxy (no direct-egress fallback)"
            )
        return proxy

    def get_json(self, url: str) -> dict:  # pragma: no cover - live network path
        _assert_official_url(url)
        proxy = self._proxy()
        # Imported lazily so the module has no hard runtime dep while inert.
        import urllib.request

        handler = urllib.request.ProxyHandler({"https": proxy})
        opener = urllib.request.build_opener(handler)
        with opener.open(url, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))


@dataclass(frozen=True)
class RawLawDocument:
    """A single law document as fetched + parsed, before statutory extraction."""

    source_system: str
    source_url: str
    jurisdiction: str
    title: str          # human title, e.g. "29 U.S.C. § 552"
    citation: str       # canonical citation label
    effective: str      # effective date / version
    retrieved: str      # retrieval timestamp (UTC ISO8601)
    text: str           # raw law text (may still contain annotations)
    official_source: bool = True
    extra: dict = field(default_factory=dict)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SourceAdapter:
    """Base adapter: build an allowlisted URL, fetch, parse to a RawLawDocument."""

    SOURCE_SYSTEM: str = ""
    HOST: str = ""

    def build_url(self, query: LawQuery) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def parse(self, query: LawQuery, url: str, payload: dict) -> RawLawDocument:  # pragma: no cover
        raise NotImplementedError

    def fetch(self, query: LawQuery, transport: HttpTransport) -> RawLawDocument:
        query = query.validated()
        url = _assert_official_url(self.build_url(query))
        payload = transport.get_json(url)
        return self.parse(query, url, payload)

    # -- shared parse helpers -------------------------------------------------
    def _official(self, url: str) -> bool:
        return urlsplit(url).hostname in LAW_OFFICIAL_SOURCE_HOSTS


class GovInfoAdapter(SourceAdapter):
    """GPO govinfo — U.S. Code / CFR / Public Laws (federal, public domain)."""

    SOURCE_SYSTEM = "govinfo"
    HOST = "api.govinfo.gov"

    def build_url(self, query: LawQuery) -> str:
        c = query.citation
        # A govinfo package granule; identifier is the package id when known,
        # else composed from collection + title + section.
        pkg = c.identifier or "-".join(x for x in (c.collection, c.title, c.section) if x)
        return f"https://{self.HOST}/packages/{pkg}/summary"

    def parse(self, query: LawQuery, url: str, payload: dict) -> RawLawDocument:
        text = str(payload.get("text") or payload.get("body") or "")
        title = str(payload.get("title") or query.citation.label())
        effective = str(
            payload.get("dateIssued")
            or payload.get("lastModified")
            or query.citation.version_date
            or ""
        )
        citation = str(payload.get("citation") or query.citation.label())
        return RawLawDocument(
            source_system=self.SOURCE_SYSTEM,
            source_url=url,
            jurisdiction=query.jurisdiction,
            title=title,
            citation=citation,
            effective=effective,
            retrieved=_utc_now(),
            text=text,
            official_source=self._official(url),
            extra={"collection": query.citation.collection},
        )


class CongressAdapter(SourceAdapter):
    """Library of Congress congress.gov — bills / enacted public laws (federal)."""

    SOURCE_SYSTEM = "congress"
    HOST = "api.congress.gov"

    def build_url(self, query: LawQuery) -> str:
        c = query.citation
        # e.g. /v3/law/118/pub/1  (congress / lawType / number) when identifier
        # encodes it, else a bill lookup by congress + identifier.
        ident = c.identifier or c.section or ""
        congress = c.congress or ""
        return f"https://{self.HOST}/v3/law/{congress}/{ident}".rstrip("/")

    def parse(self, query: LawQuery, url: str, payload: dict) -> RawLawDocument:
        law = payload.get("law") or payload.get("bill") or payload
        text = str(law.get("text") or law.get("fullText") or "")
        title = str(law.get("title") or query.citation.label())
        effective = str(law.get("enactedDate") or law.get("updateDate") or query.citation.version_date or "")
        citation = str(law.get("number") or law.get("citation") or query.citation.label())
        return RawLawDocument(
            source_system=self.SOURCE_SYSTEM,
            source_url=url,
            jurisdiction=query.jurisdiction,
            title=title,
            citation=citation,
            effective=effective,
            retrieved=_utc_now(),
            text=text,
            official_source=self._official(url),
        )


class ECFRAdapter(SourceAdapter):
    """eCFR — electronic Code of Federal Regulations (federal, public domain)."""

    SOURCE_SYSTEM = "ecfr"
    HOST = "api.ecfr.gov"

    def build_url(self, query: LawQuery) -> str:
        c = query.citation
        date = c.version_date or "current"
        # /api/versioner/v1/full/{date}/title-{title}.json?part=...&section=...
        base = f"https://{self.HOST}/api/versioner/v1/full/{date}/title-{c.title}.json"
        params = []
        if c.part:
            params.append(f"part={c.part}")
        if c.section:
            params.append(f"section={c.section}")
        return base + ("?" + "&".join(params) if params else "")

    def parse(self, query: LawQuery, url: str, payload: dict) -> RawLawDocument:
        text = str(payload.get("text") or payload.get("content") or "")
        title = str(payload.get("label") or payload.get("title") or query.citation.label())
        effective = str(payload.get("date") or query.citation.version_date or "current")
        citation = str(payload.get("citation") or query.citation.label())
        return RawLawDocument(
            source_system=self.SOURCE_SYSTEM,
            source_url=url,
            jurisdiction=query.jurisdiction,
            title=title,
            citation=citation,
            effective=effective,
            retrieved=_utc_now(),
            text=text,
            official_source=self._official(url),
        )


ADAPTERS = {
    GovInfoAdapter.SOURCE_SYSTEM: GovInfoAdapter,
    CongressAdapter.SOURCE_SYSTEM: CongressAdapter,
    ECFRAdapter.SOURCE_SYSTEM: ECFRAdapter,
}


def adapter_for(source_system: str) -> SourceAdapter:
    cls = ADAPTERS.get((source_system or "").strip().lower())
    if cls is None:
        raise KeyError(f"no adapter for source_system {source_system!r}")
    return cls()
