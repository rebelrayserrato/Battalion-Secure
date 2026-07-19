"""Web-connected law ingest pipeline (RAYAAAA-274, Phase P2 of RAYAAAA-270).

Owner ask (RAYAAAA-191): let the app search *official government sources* and add
the returned statute / regulation text to the RAYAAAA-251 Law Library. This
package is the pipeline that does the fetch + parse + clean + provenance + stage
half of that ask. It is deliberately built so that, even when live, it can only:

* reach the exact official hosts in ``LAW_OFFICIAL_SOURCE_HOSTS`` (govinfo /
  congress.gov / eCFR) over the RAYAAAA-273 egress proxy — nothing else;
* put a STRUCTURED, no-PII query (jurisdiction + citation) on the wire — free
  text never leaves the box (Counsel/CTO Condition C), enforced by the
  :mod:`~review_engine.law.web.query` contract, not by post-filtering;
* ingest STATUTORY TEXT ONLY (public domain — 17 U.S.C. §105; *Georgia v.
  Public.Resource.Org*), with West/Lexis-style annotations stripped and flagged
  (Counsel Condition B);
* write into a Pending-Review STAGING area (``LAW_STAGING_DIR``) — never the live
  index — for owner approval via the RAYAAAA-275 UI (Counsel Condition D / CTO 5).

It is FLAG-OFF / INERT by default (``LAW_WEB_INGEST_ENABLED``); with the flag off
the pipeline refuses to run at all. SYNTHETIC / owner-internal only until the
Phase-4 real-PII gate; this pipeline does NOT advance that gate.
"""

from review_engine.law.web.adapters import (
    ADAPTERS,
    CongressAdapter,
    ECFRAdapter,
    EgressBlocked,
    GovInfoAdapter,
    HttpTransport,
    MissingCredential,
    ProxyHttpTransport,
    RawLawDocument,
    SourceAdapter,
    adapter_for,
)
from review_engine.law.web.extraction import (
    LawSegment,
    StatutoryExtraction,
    extract_statutory,
)
from review_engine.law.web.pipeline import (
    FeatureDisabled,
    IngestResult,
    JurisdictionLeak,
    WebLawIngestPipeline,
)
from review_engine.law.web.provenance import WebLawProvenance
from review_engine.law.web.query import (
    Citation,
    LawQuery,
    NoPIIViolation,
    SOURCE_SYSTEMS,
)
from review_engine.law.web.staging import (
    StagedLawDocument,
    StagingSink,
    WebLawStagingStore,
)

__all__ = [
    "ADAPTERS",
    "Citation",
    "CongressAdapter",
    "ECFRAdapter",
    "EgressBlocked",
    "FeatureDisabled",
    "GovInfoAdapter",
    "HttpTransport",
    "IngestResult",
    "JurisdictionLeak",
    "MissingCredential",
    "LawQuery",
    "LawSegment",
    "NoPIIViolation",
    "ProxyHttpTransport",
    "RawLawDocument",
    "SOURCE_SYSTEMS",
    "SourceAdapter",
    "StagedLawDocument",
    "StagingSink",
    "StatutoryExtraction",
    "WebLawIngestPipeline",
    "WebLawProvenance",
    "WebLawStagingStore",
    "adapter_for",
    "extract_statutory",
]
