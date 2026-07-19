"""The no-PII structured law-query contract (RAYAAAA-274 P2 ⇄ RAYAAAA-273 P1).

Counsel Condition C / CTO Condition C: *free text must never cross the egress
proxy*. Only a structured ``jurisdiction + citation`` query may. RAYAAAA-273 (P1)
builds a :class:`LawQuery` from the owner's request LOCALLY (the owner's free-text
prompt is consumed on-box and turned into these structured fields); the outbound
adapters in this package accept ONLY a validated :class:`LawQuery` and refuse
anything else, so an adapter can never be handed raw free text to place on the
wire. This is the technical enforcement of the "no-PII in query" control — it
lives at the *contract* boundary, not in a post-hoc scrub.

Every field is a citation-shaped token (a title/part/section number, a chamber, a
bill number, a date). We validate each against a strict allowlist charset and a
short length bound and forbid a free-text field entirely, so a caller physically
cannot smuggle a sentence — let alone PII — through this object.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from review_engine.law.library import validate_law_jurisdiction

# The three official structured-API source systems P2 supports (Counsel/CTO
# scoped these to official US-gov publishers of public-domain law).
SOURCE_SYSTEMS = ("govinfo", "congress", "ecfr")

# A citation token may contain digits, letters, spaces and the punctuation that
# appears in real citations (``.`` ``-`` ``/`` ``§``). It may NOT contain the
# characters that free-text / PII would need (``@``, quotes, most punctuation),
# and is length-bounded. This is intentionally strict: it is a *citation locator*,
# never a search phrase.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9 .\-/§]+$")
_MAX_TOKEN_LEN = 64
_MAX_TOKEN_WORDS = 8  # a citation locator, not a sentence


class NoPIIViolation(ValueError):
    """Raised when a query field is not a safe, structured citation token.

    Fail-closed: anything that does not look like a citation locator is rejected
    before it can be built into an outbound URL.
    """


def _validate_token(name: str, value: str, *, required: bool) -> str:
    token = (value or "").strip()
    if not token:
        if required:
            raise NoPIIViolation(f"{name} is required and must be non-empty")
        return ""
    if len(token) > _MAX_TOKEN_LEN:
        raise NoPIIViolation(
            f"{name} exceeds {_MAX_TOKEN_LEN} chars — refusing (looks like free text, not a citation)"
        )
    if len(token.split()) > _MAX_TOKEN_WORDS:
        raise NoPIIViolation(
            f"{name} has too many words — refusing (looks like free text, not a citation)"
        )
    if not _TOKEN_RE.match(token):
        raise NoPIIViolation(
            f"{name} contains characters not allowed in a citation locator: {value!r}"
        )
    return token


@dataclass(frozen=True)
class Citation:
    """A structured citation locator — the ONLY law-identifying payload allowed
    on the wire. Which fields are meaningful depends on the source system, but in
    all cases every field is a validated citation token (never free text)."""

    title: str = ""       # e.g. "29" (USC/CFR title) or "" for a bill
    part: str = ""        # e.g. CFR part "1630"
    section: str = ""     # e.g. "552" / "1630.2"
    collection: str = ""  # e.g. govinfo collection "USCODE" / "CFR" / "PLAW"
    identifier: str = ""  # e.g. govinfo package id, or congress bill "hr-1"
    congress: str = ""    # e.g. "118" (congress.gov)
    version_date: str = ""  # e.g. "2023-01-01" (point-in-time version)

    def validated(self) -> "Citation":
        return Citation(
            title=_validate_token("title", self.title, required=False),
            part=_validate_token("part", self.part, required=False),
            section=_validate_token("section", self.section, required=False),
            collection=_validate_token("collection", self.collection, required=False),
            identifier=_validate_token("identifier", self.identifier, required=False),
            congress=_validate_token("congress", self.congress, required=False),
            version_date=_validate_token("version_date", self.version_date, required=False),
        )

    def is_empty(self) -> bool:
        return not any(
            (self.title, self.part, self.section, self.collection, self.identifier, self.congress)
        )

    def label(self) -> str:
        """Human citation label (for the staged document name / audit)."""
        bits = []
        if self.title:
            bits.append(f"Title {self.title}")
        if self.collection:
            bits.append(self.collection)
        if self.part:
            bits.append(f"Part {self.part}")
        if self.section:
            bits.append(f"§ {self.section}")
        if self.identifier and not bits:
            bits.append(self.identifier)
        if self.congress:
            bits.append(f"{self.congress}th Cong.")
        return " ".join(bits) or (self.identifier or "citation")


@dataclass(frozen=True)
class LawQuery:
    """A validated, no-PII outbound law query: a jurisdiction + a citation +
    which official source system to ask. There is deliberately no free-text
    field — this object is all that P2 will ever put on the wire."""

    jurisdiction: str
    source_system: str
    citation: Citation = field(default_factory=Citation)

    def validated(self) -> "LawQuery":
        jur = validate_law_jurisdiction(self.jurisdiction)
        system = (self.source_system or "").strip().lower()
        if system not in SOURCE_SYSTEMS:
            raise NoPIIViolation(
                f"unknown source_system {self.source_system!r}; expected one of {SOURCE_SYSTEMS}"
            )
        citation = self.citation.validated()
        if citation.is_empty():
            raise NoPIIViolation(
                "citation is empty — a structured citation locator is required "
                "(free-text search is not permitted on the outbound path)"
            )
        return LawQuery(jurisdiction=jur, source_system=system, citation=citation)
