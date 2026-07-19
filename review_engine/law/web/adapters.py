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

Response formats differ per source (verified live 2026-07-19 through the
RAYAAAA-273 egress proxy, RAYAAAA-289):

* **eCFR** serves the ``/versioner/v1/full/`` endpoint as **XML only** — the
  ``.json`` variant returns HTTP 406. So :class:`ECFRAdapter` fetches XML
  (``transport.get_text``) and parses the ``DIV*``/``HEAD``/``P`` section
  structure. eCFR is KEYLESS.
* **govinfo / congress** are JSON APIs behind the shared ``api.data.gov`` key.
  The key is env-only (Counsel C-6: ``DATA_GOV_API_KEY`` / ``CONGRESS_GOV_API_KEY``)
  and is injected by the transport at send time — never built into the no-PII
  query, never a source literal. Absent a key the transport fails closed with a
  clear :class:`MissingCredential` so the adapter is inert (owner sees a message,
  never a crash).
"""
from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from review_engine.config.settings import LAW_OFFICIAL_SOURCE_HOSTS
from review_engine.law.library import FEDERAL_JURISDICTION
from review_engine.law.web.query import Citation, LawQuery


class EgressBlocked(RuntimeError):
    """Raised when an outbound URL is not an allowlisted official host over https."""


class MissingCredential(EgressBlocked):
    """Raised when a source needs an env-only API key that is not provisioned.

    Subclasses :class:`EgressBlocked` so callers that already fail-close on egress
    problems treat a missing credential the same way — the adapter stays inert and
    nothing is put on the wire. (Counsel C-6: keys are env-only.)
    """


# Hosts that require the shared ``api.data.gov`` API key on the query string.
# eCFR is deliberately absent — it is keyless.
_API_KEY_HOSTS = ("api.govinfo.gov", "api.congress.gov")


def _api_keys_from_env() -> dict:
    """Load the env-only (Counsel C-6) API keys, keyed by host. Never from source.

    ``api.data.gov`` issues one shared key that both govinfo and congress accept;
    ``DATA_GOV_API_KEY`` covers both, and ``CONGRESS_GOV_API_KEY`` may override the
    congress host specifically. Absent keys simply leave the host unmapped, and the
    transport then fails closed (:class:`MissingCredential`) when that host is hit.
    """
    data_gov = (os.getenv("DATA_GOV_API_KEY") or "").strip()
    congress = (os.getenv("CONGRESS_GOV_API_KEY") or "").strip() or data_gov
    keys: dict = {}
    if data_gov:
        keys["api.govinfo.gov"] = data_gov
    if congress:
        keys["api.congress.gov"] = congress
    return keys


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
    """Fetches a document from an allowlisted official host.

    Implementations MUST route through the RAYAAAA-273 egress proxy and MUST
    re-assert the host allowlist; the query builder guarantees no PII is in the
    URL, and the transport guarantees the URL only reaches an official host.

    Two accessors: :meth:`get_json` for the JSON APIs (govinfo / congress) and
    :meth:`get_text` for raw bodies (eCFR serves XML, not JSON)."""

    def get_json(self, url: str) -> dict: ...

    def get_text(self, url: str) -> str: ...


class ProxyHttpTransport:
    """Live transport: https-only, through ``HTTPS_PROXY``, allowlist-enforced.

    Not used while the feature is flag-off (the pipeline never constructs it).
    When enabled it requires an egress proxy to be configured — there is no
    direct-egress fallback, so if the RAYAAAA-273 proxy is absent it fails closed.

    For the api.data.gov-keyed hosts (govinfo / congress) it injects the env-only
    API key onto the query string at send time; the key is never part of the
    no-PII query object and never a source literal (Counsel C-6). eCFR is keyless.
    """

    def __init__(self, *, timeout: float = 15.0, api_keys: dict | None = None):
        self.timeout = timeout
        # API keys (govinfo/congress) come from the environment only, never source.
        self._api_keys = _api_keys_from_env() if api_keys is None else dict(api_keys)

    def _proxy(self) -> str:
        proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
        if not proxy:
            raise EgressBlocked(
                "HTTPS_PROXY is not set — refusing to egress without the "
                "RAYAAAA-273 default-deny proxy (no direct-egress fallback)"
            )
        return proxy

    def _with_api_key(self, url: str) -> str:
        """Append the env-only api.data.gov key for the keyed hosts; fail closed."""
        host = urlsplit(url).hostname
        if host not in _API_KEY_HOSTS:
            return url  # keyless (eCFR) — nothing to add
        key = self._api_keys.get(host)
        if not key:
            raise MissingCredential(
                f"{host} requires an api.data.gov API key (env DATA_GOV_API_KEY / "
                "CONGRESS_GOV_API_KEY, Counsel C-6) — not provisioned; this source "
                "is inert until the key is set on review-engine"
            )
        sep = "&" if urlsplit(url).query else "?"
        return f"{url}{sep}api_key={key}"

    def get_text(self, url: str) -> str:  # pragma: no cover - live network path
        _assert_official_url(url)
        # Proxy presence is checked BEFORE the key so a missing default-deny proxy
        # is the first failure (no-direct-egress guarantee, RAYAAAA-273).
        proxy = self._proxy()
        url = self._with_api_key(url)
        # Imported lazily so the module has no hard runtime dep while inert.
        import urllib.request

        handler = urllib.request.ProxyHandler({"https": proxy})
        opener = urllib.request.build_opener(handler)
        with opener.open(url, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8")

    def get_json(self, url: str) -> dict:  # pragma: no cover - live network path
        return json.loads(self.get_text(url))


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
        return self.fetch_document(query, url, transport)

    def fetch_document(
        self, query: LawQuery, url: str, transport: HttpTransport
    ) -> RawLawDocument:
        """Fetch + parse the document. Default is the JSON APIs (govinfo/congress);
        eCFR overrides this to fetch XML text (its ``/full/`` is XML-only)."""
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


def _default_ecfr_date() -> str:
    """eCFR ``/full/`` wants a concrete ``YYYY-MM-DD`` (not ``current``); default
    to today (UTC), which returns the CFR as in effect now."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# eCFR XML apparatus we drop at parse time: editorial notes, footnotes, and the
# source/authority citation lines are not the operative regulatory text
# (Counsel B statutory-only floor). HEAD/HED/P carry the regulation itself.
_ECFR_SKIP_TAGS = {"NOTE", "FTNT", "CITA", "AUTH", "SOURCE", "EDNOTE", "SECAUTH"}
_ECFR_TEXT_TAGS = {"HEAD", "HED", "P"}


class ECFRAdapter(SourceAdapter):
    """eCFR — electronic Code of Federal Regulations (federal, public domain).

    The ``/versioner/v1/full/`` endpoint is **XML-only** (the ``.json`` variant
    returns HTTP 406, verified live 2026-07-19). We fetch the XML and parse the
    ``DIV*`` → ``HEAD``/``P`` section structure into statutory text. eCFR is
    KEYLESS, so this adapter works with no api.data.gov key.
    """

    SOURCE_SYSTEM = "ecfr"
    HOST = "api.ecfr.gov"

    def build_url(self, query: LawQuery) -> str:
        c = query.citation
        # Pure URL builder (no network) — used for the allowlist check and when a
        # version_date is given. Without one it defaults to today; the live fetch
        # path resolves eCFR's actual latest available date instead (see fetch()).
        return self._full_url(c.version_date or _default_ecfr_date(), c)

    def _full_url(self, date: str, c: Citation) -> str:
        # /api/versioner/v1/full/{date}/title-{title}.xml?part=...&section=...
        # (api.ecfr.gov 302-redirects to www.ecfr.gov; both are allowlisted, so
        # the RAYAAAA-273 proxy permits the redirect.)
        base = f"https://{self.HOST}/api/versioner/v1/full/{date}/title-{c.title}.xml"
        params = []
        if c.part:
            params.append(f"part={c.part}")
        if c.section:
            params.append(f"section={c.section}")
        return base + ("?" + "&".join(params) if params else "")

    def fetch(self, query: LawQuery, transport: HttpTransport) -> RawLawDocument:
        query = query.validated()
        c = query.citation
        # eCFR's ``/full/{date}`` needs a date that actually has a published
        # version — a future/too-recent date 404s. When the owner didn't pin a
        # version_date, ask eCFR for the title's latest available date rather than
        # guessing "today" (which lags the real corpus).
        date = c.version_date or self._latest_available_date(c.title, transport)
        url = _assert_official_url(self._full_url(date, c))
        xml_text = transport.get_text(url)
        return self.parse_xml(query, url, xml_text, effective=date)

    def _latest_available_date(self, title: str, transport: HttpTransport) -> str:
        """The most recent date eCFR has a published version of ``title`` for.

        Reads the keyless ``titles.json`` (same allowlisted host, no new egress
        target). Falls back to today if the lookup fails — the fetch then surfaces
        any resulting error to the owner rather than crashing.
        """
        try:
            data = transport.get_json(
                f"https://{self.HOST}/api/versioner/v1/titles.json"
            )
            for entry in data.get("titles", []):
                if str(entry.get("number")) == str(title):
                    date = entry.get("up_to_date_as_of") or entry.get("latest_issue_date")
                    if date:
                        return str(date)
        except Exception:  # network/parse issue — fall back, don't crash the fetch
            pass
        return _default_ecfr_date()

    def parse_xml(
        self, query: LawQuery, url: str, xml_text: str, effective: str | None = None
    ) -> RawLawDocument:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:  # malformed body -> surface, not crash
            raise ValueError(f"eCFR returned unparseable XML from {url!r}: {exc}") from exc

        blocks: list[str] = []
        self._collect_text(root, blocks)
        # Blank-line-separate the blocks so the statutory-only extractor
        # (Counsel B) can split them; HEAD lines start each section.
        text = "\n\n".join(blocks).strip()

        heading = self._first_head(root)
        citation = self._citation_from_metadata(root) or query.citation.label()
        title = heading or citation
        effective = effective or query.citation.version_date or _default_ecfr_date()
        return RawLawDocument(
            source_system=self.SOURCE_SYSTEM,
            source_url=url,
            # eCFR is a FEDERAL source; asserting FEDERAL here means a non-federal
            # query is caught by the pipeline's jurisdiction hard-filter (251 AC-C).
            jurisdiction=FEDERAL_JURISDICTION,
            title=title,
            citation=citation,
            effective=effective,
            retrieved=_utc_now(),
            text=text,
            official_source=self._official(url),
        )

    # -- XML helpers ----------------------------------------------------------
    @staticmethod
    def _tag(el) -> str:
        return el.tag.split("}")[-1].upper() if isinstance(el.tag, str) else ""

    def _collect_text(self, el, out: list) -> None:
        """Depth-first, document-order walk collecting HEAD/P text; prune the
        editorial apparatus subtrees (NOTE/CITA/AUTH/…) entirely."""
        tag = self._tag(el)
        if tag in _ECFR_SKIP_TAGS:
            return
        if tag in _ECFR_TEXT_TAGS:
            txt = " ".join("".join(el.itertext()).split())
            if txt:
                out.append(txt)
            return  # leaf content captured; don't descend again
        for child in el:
            self._collect_text(child, out)

    def _first_head(self, root) -> str:
        for el in root.iter():
            if self._tag(el) == "HEAD":
                txt = " ".join("".join(el.itertext()).split())
                if txt:
                    return txt
        return ""

    @staticmethod
    def _citation_from_metadata(root) -> str:
        """eCFR stamps a ``hierarchy_metadata`` JSON blob with a ``citation`` (e.g.
        ``29 CFR 1630.2``) on the DIV; use it when present."""
        for el in root.iter():
            meta = el.attrib.get("hierarchy_metadata")
            if meta:
                try:
                    cite = json.loads(meta).get("citation")
                except (ValueError, TypeError):
                    cite = None
                if cite:
                    return str(cite)
        return ""


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
