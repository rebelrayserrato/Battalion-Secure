"""No-PII outbound query boundary for official-government-source law lookups.

RAYAAAA-273 (P1 foundation of RAYAAAA-270). This module is the ONLY place in the
review-engine that constructs an outbound HTTP request destined for the public
internet (the RAYAAAA-255 default-deny egress proxy). It exists to make Counsel
Condition C (RAYAAAA-271) and CTO Condition 3 (RAYAAAA-272) STRUCTURAL rather
than a matter of discipline:

    Free-text search stays LOCAL. Only a structured {jurisdiction, citation,
    topic} lookup may cross the proxy — and it may cross ONLY to an official
    government publisher on the exact-hostname allowlist.

How the no-PII guarantee is enforced (defence in depth):

1. :class:`LawWebQuery` is a *frozen* dataclass with EXACTLY three fields. There
   is no field that can hold matter text, a client/matter identifier, or
   uploaded-document content, and an unknown constructor keyword raises
   ``TypeError`` — so such data cannot be attached to an outbound query at all.
2. Every field is validated to a structured shape in ``__post_init__``:
   ``jurisdiction`` to a canonical law-jurisdiction code, ``citation`` to a
   citation-shaped token (no free-text sentences), ``topic`` to a member of a
   controlled vocabulary. Free-text (and therefore any smuggled PII) is rejected.
3. :func:`build_outbound_request` builds the wire request from a fixed mapping of
   those three fields plus an env-only API key. No caller-supplied dict is ever
   forwarded, so no extra key can ride along.
4. :func:`assert_no_pii_leak` is a runtime belt-and-suspenders guard the ingest
   caller runs with the matter's known PII values; it re-scans the fully rendered
   request and fail-closes if any of them appear.

Ships INERT: :func:`is_law_web_ingest_enabled` is OFF unless ``LAW_WEB_INGEST_ENABLED``
is set, and no execution path in this module opens a socket — it only *builds* the
request object. The actual fetch/ingest wiring (RAYAAAA-274) and staging UI
(RAYAAAA-275) live behind that flag and the CTO-reviewed egress cutover.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

from review_engine.law.library import (
    FEDERAL_JURISDICTION,
    validate_law_jurisdiction,
)

# --- Feature flag (OFF by default; INERT until the CTO-reviewed cutover) ------
LAW_WEB_INGEST_FLAG = "LAW_WEB_INGEST_ENABLED"


def is_law_web_ingest_enabled() -> bool:
    """True only if the operator has explicitly enabled web law ingest.

    Ships OFF. The builder functions below are pure and testable regardless, but
    any code that would actually *perform* a fetch must gate on this.
    """
    return os.getenv(LAW_WEB_INGEST_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def ensure_web_ingest_enabled() -> None:
    """Raise unless web law ingest is enabled. Call before any real fetch."""
    if not is_law_web_ingest_enabled():
        raise RuntimeError(
            "Web law ingest is disabled. Set "
            f"{LAW_WEB_INGEST_FLAG}=1 only after the RAYAAAA-272 egress cutover."
        )


# --- The ONLY fields allowed to cross the wire (Counsel Condition C) ----------
ALLOWED_QUERY_FIELDS: frozenset[str] = frozenset({"jurisdiction", "citation", "topic"})

# A citation must look like a citation, never a sentence. Permitted: digits,
# letters, spaces and the punctuation that appears in reporter citations
# (``42 U.S.C. § 1983``, ``29 C.F.R. 1604.11``, ``Art. 5(1)(a)``). Capped short.
_CITATION_RE = re.compile(r"^[A-Za-z0-9 .§/()\-]{1,64}$")

# A topic is NOT free text: it must be a member of the controlled vocabulary
# below, so a name / matter phrase can never be passed as a "topic". Extend this
# list deliberately (it is reviewable) as real jurisdictions/subjects are added.
ALLOWED_TOPICS: frozenset[str] = frozenset(
    {
        "civil rights",
        "employment",
        "labor",
        "privacy",
        "data protection",
        "consumer protection",
        "contracts",
        "corporations",
        "criminal",
        "environmental",
        "evidence",
        "family",
        "health",
        "immigration",
        "intellectual property",
        "insurance",
        "procedure",
        "property",
        "securities",
        "tax",
        "torts",
    }
)


@dataclass(frozen=True)
class LawWebQuery:
    """A structured, PII-free law lookup — the ONLY thing that may cross the proxy.

    ``jurisdiction`` is required; at least one of ``citation`` / ``topic`` must be
    given. All three are validated to a structured shape on construction, so an
    instance can never carry free text (and therefore never carry PII). Frozen, so
    no field can be mutated or added after construction.
    """

    jurisdiction: str
    citation: Optional[str] = None
    topic: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "jurisdiction", validate_law_jurisdiction(self.jurisdiction))

        if self.citation is not None:
            token = self.citation.strip()
            if not _CITATION_RE.fullmatch(token):
                raise ValueError(
                    "citation is not a structured citation token (letters, digits, "
                    "spaces and citation punctuation only, <= 64 chars); free text is "
                    "rejected so it cannot carry matter content or PII."
                )
            object.__setattr__(self, "citation", token)

        if self.topic is not None:
            token = self.topic.strip().lower()
            if token not in ALLOWED_TOPICS:
                raise ValueError(
                    f"topic {self.topic!r} is not in the controlled legal-topic "
                    "vocabulary; free-text topics are rejected (Counsel Condition C)."
                )
            object.__setattr__(self, "topic", token)

        if self.citation is None and self.topic is None:
            raise ValueError("a LawWebQuery must have at least one of citation / topic.")


@dataclass(frozen=True)
class OfficialSource:
    """One allowlisted OFFICIAL government publisher (never a vendor mirror).

    ``host`` MUST also appear, verbatim, in the RAYAAAA-273 egress allowlist
    (docker/egress-proxy/allowlist.filter) — this registry and that file are the
    two halves of the same exact-hostname control and are kept in sync by review.
    """

    key: str
    publisher: str
    host: str
    path: str
    jurisdictions: frozenset[str]
    api_key_env: Optional[str] = None          # env var name; NEVER a literal key
    api_key_param: Optional[str] = None         # query-param name for the key, or
    api_key_header: Optional[str] = None        # header name for the key
    structured_api: bool = True                 # prefer these over HTML (Counsel A)

    def serves(self, jurisdiction: str) -> bool:
        return jurisdiction in self.jurisdictions


# Launch set: FEDERAL official publishers only. State hosts are added later,
# per-jurisdiction, via the same small git-tracked diff (CTO Condition 7).
# Each host below is the OFFICIAL publisher (verified at add-time, Counsel A):
#   * api.govinfo.gov  — U.S. GPO GovInfo API (statutes/CFR compilations)
#   * api.congress.gov — Library of Congress / GPO (bill & statute metadata)
#   * api.ecfr.gov     — GPO/OFR electronic CFR API (regulatory text)
_SOURCES: dict[str, OfficialSource] = {
    src.key: src
    for src in (
        OfficialSource(
            key="govinfo",
            publisher="U.S. Government Publishing Office (GovInfo)",
            host="api.govinfo.gov",
            path="/search",
            jurisdictions=frozenset({FEDERAL_JURISDICTION}),
            api_key_env="DATA_GOV_API_KEY",       # api.data.gov shared key
            api_key_param="api_key",
        ),
        OfficialSource(
            key="congress",
            publisher="Library of Congress (congress.gov API)",
            host="api.congress.gov",
            path="/v3/law",
            jurisdictions=frozenset({FEDERAL_JURISDICTION}),
            api_key_env="CONGRESS_GOV_API_KEY",
            api_key_param="api_key",
        ),
        OfficialSource(
            key="ecfr",
            publisher="Office of the Federal Register (eCFR API)",
            host="api.ecfr.gov",
            path="/api/search/v1/results",
            jurisdictions=frozenset({FEDERAL_JURISDICTION}),
            # eCFR API requires no key.
        ),
    )
}


def official_sources() -> dict[str, OfficialSource]:
    """The registered official publishers (copy; keyed by source key)."""
    return dict(_SOURCES)


def get_official_source(key: str) -> OfficialSource:
    src = _SOURCES.get(key)
    if src is None:
        raise ValueError(
            f"{key!r} is not a registered OFFICIAL law publisher; outbound law "
            "lookups may target allowlisted government publishers only."
        )
    return src


@dataclass(frozen=True)
class OutboundRequest:
    """A fully-rendered, inspectable outbound request. GET-only, HTTPS-only."""

    method: str
    url: str
    params: dict
    headers: dict

    def wire_text(self) -> str:
        """Every byte that would leave the box, concatenated for scanning."""
        parts = [self.method, self.url]
        parts += [f"{k}={v}" for k, v in self.params.items()]
        parts += [f"{k}: {v}" for k, v in self.headers.items()]
        return "\n".join(str(p) for p in parts)

    def full_url(self) -> str:
        if not self.params:
            return self.url
        return f"{self.url}?{urlencode(self.params)}"


_USER_AGENT = "RAYSERR-Lens-LawLibrary/1.0 (+official-source ingest; contact owner)"


def build_outbound_request(query: LawWebQuery, source_key: str) -> OutboundRequest:
    """Build the outbound request for ``query`` against an official publisher.

    Structural no-PII boundary: the returned request is assembled ONLY from the
    validated ``query`` fields and the env-only API key. No caller-provided dict
    is forwarded. HTTPS + GET only (the egress proxy also allows CONNECT :443 only).
    """
    src = get_official_source(source_key)
    if not src.serves(query.jurisdiction):
        raise ValueError(
            f"{src.key!r} ({src.publisher}) does not serve jurisdiction "
            f"{query.jurisdiction!r}."
        )

    # Params are built from a FIXED mapping of the three allowed fields only.
    params: dict[str, str] = {}
    if query.citation is not None:
        params["citation"] = query.citation
    if query.topic is not None:
        params["query"] = query.topic
    # Jurisdiction is structural context for the endpoint; include it explicitly.
    params["jurisdiction"] = query.jurisdiction

    headers: dict[str, str] = {"Accept": "application/json", "User-Agent": _USER_AGENT}

    # API key: env only, NEVER a literal. Fail closed if a key is required but unset
    # (better to not fetch than to fetch unauthenticated / rate-limited).
    if src.api_key_env:
        api_key = os.getenv(src.api_key_env, "").strip()
        if not api_key:
            raise RuntimeError(
                f"{src.key!r} requires an API key in ${src.api_key_env}; refusing to "
                "build an outbound request without it."
            )
        if src.api_key_param:
            params[src.api_key_param] = api_key
        elif src.api_key_header:
            headers[src.api_key_header] = api_key

    url = f"https://{src.host}{src.path}"
    return OutboundRequest(method="GET", url=url, params=params, headers=headers)


def assert_no_pii_leak(request: OutboundRequest, forbidden_values) -> None:
    """Fail closed if any known PII / matter value appears in the wire request.

    Defence in depth on top of the structural boundary: the ingest caller passes
    the matter's known sensitive strings (client name, matter text, identifiers,
    uploaded-doc snippets). If any non-trivial one is present in the rendered
    request, this raises before anything is sent.
    """
    haystack = request.wire_text().lower()
    for value in forbidden_values or ():
        needle = str(value or "").strip().lower()
        if len(needle) < 3:
            continue  # too short to be a meaningful identifier; avoid false positives
        if needle in haystack:
            raise RuntimeError(
                "Refusing outbound law request: a matter/PII value would leak into "
                "the outbound request. This must never happen — the structured "
                "query boundary was bypassed."
            )
