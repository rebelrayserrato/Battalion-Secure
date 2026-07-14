"""Assistant-surface security hardening (RAYAAAA-256, Phase C Sec/QA gate).

This module is the security boundary for the cross-Task assistant + MCP connector
egress surface (RAYAAAA-246/247/248), added ahead of the RAYAAAA-253 Sec/QA gate
that must clear before any *real* PII may leave the sealed review-engine network.
It has two coherent halves on the same surface:

PART A — access control (controls C1/C2)
----------------------------------------
* **RBAC** — the assistant is reachable only by an authenticated principal whose
  role is in the authorized set. This layers on top of the network-level
  RAYAAAA-205 authz route (nginx ``auth_request`` never proxies the Streamlit
  app / matter API without a valid owner session); here we additionally enforce
  the *role* app-side, so there is no in-app path to the assistant/connector for
  a session that authenticated but lacks the role.
* **MFA** — a second factor must have been satisfied for the session (Phase-4
  open item RAYAAAA-229-P4). We do not re-implement WebAuthn/TOTP here; we consume
  the assertion the RAYAAAA-136 auth stack / authz route forwards (an ``amr``-style
  header) and fail closed when it is absent. MFA is scoped to *this* surface.

The principal is resolved from the request headers the upstream authz layer
forwards (``principal_from_headers``); ``enforce_access`` is the single fail-closed
gate the view and service layer call.

PART B — input handling on the egress path (controls C5/C10)
------------------------------------------------------------
Everything that would be placed into a provider payload is first run through
``harden_context``:

* **Malware scan** — each chunk is signature-scanned (EICAR test vector,
  executable magic, high-signal script/macro markers). A hit *quarantines* the
  chunk: it is dropped and never reaches the payload. Extensible to an external
  scanner (ClamAV) via ``ASSISTANT_MALWARE_CMD`` without changing callers.
* **Prompt-injection guard** — retrieved document text is untrusted DATA, not
  instructions. Injection markers ("ignore previous instructions", role-label
  spoofing, exfiltration verbs, …) are *defanged* in place so document content
  cannot coerce the model into revealing or exfiltrating other context. This is
  belt-and-braces with the instruction-isolation clause in the assistant system
  prompt.
* **Data minimization** (Schrems II supplementary measure #1) — only retrieved
  chunks + the prompt are egressed (never whole documents — structural, see
  ``MultiModelAssistant.ask``); on top of that, obvious *direct identifiers*
  (emails, phone numbers, SSNs, card-like numbers) are stripped from chunk text
  before egress. Provider-specific tuning firms up after the CEO provider-set
  decision (RAYAAAA-249 §7 B-1); this is the general mechanism.

Design notes: stdlib only (no new deps, matches the connector's posture); benign
content passes through **byte-identical** so there is zero behaviour change when
nothing is flagged; the whole surface stays behind the existing OFF-by-default
flags (``CROSS_TASK_ASSISTANT_ENABLED`` / ``MCP_CONNECTOR_ENABLED``).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

from review_engine.app.cross_task import CrossTaskAccessError

# ---------------------------------------------------------------------------
# PART A — RBAC + MFA
# ---------------------------------------------------------------------------


class AssistantAccessError(CrossTaskAccessError):
    """Raised when a session may not reach the assistant surface.

    Subclasses ``CrossTaskAccessError`` so existing callers/handlers that catch
    the cross-Task access error keep working unchanged."""


# Roles allowed to reach the assistant surface. Owner-internal capability, so the
# default set is deliberately tiny. Override with a comma list in
# ``ASSISTANT_AUTHORIZED_ROLES`` (values are compared case-insensitively).
DEFAULT_AUTHORIZED_ROLES = frozenset({"owner", "admin"})

# Header names the RAYAAAA-205 authz route / RAYAAAA-136 auth stack forward once a
# session is validated. Configurable so we do not hard-code one upstream's
# convention; defaults follow the oauth2-proxy ``X-Auth-Request-*`` family.
_ROLE_HEADER = os.getenv("ASSISTANT_ROLE_HEADER", "X-Auth-Request-Role")
_MFA_HEADER = os.getenv("ASSISTANT_MFA_HEADER", "X-Auth-Request-Mfa")
_USER_HEADER = os.getenv("ASSISTANT_USER_HEADER", "X-Auth-Request-Email")

# Values in the MFA header that count as "a second factor was satisfied".
_MFA_OK_VALUES = frozenset({"1", "true", "yes", "on", "verified", "mfa", "totp", "otp", "webauthn", "2fa"})


def authorized_roles() -> frozenset[str]:
    raw = os.getenv("ASSISTANT_AUTHORIZED_ROLES")
    if not raw or not raw.strip():
        return DEFAULT_AUTHORIZED_ROLES
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Principal:
    """The authenticated identity reaching the surface, as asserted by the
    upstream authz route. ``authenticated`` is False for an anonymous/absent
    session (no identity headers forwarded)."""

    subject: str = ""
    role: str = ""
    mfa: bool = False
    authenticated: bool = False


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    """Case-insensitive header lookup (WSGI/ASGI/Streamlit all differ)."""
    if not headers:
        return None
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _mfa_satisfied(raw: Optional[str]) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in _MFA_OK_VALUES


def principal_from_headers(headers: Optional[Mapping[str, str]]) -> Principal:
    """Build a :class:`Principal` from the identity headers the authz route
    forwards. Absent headers -> an unauthenticated principal (fails closed)."""
    if not headers:
        return Principal()
    role = (_header(headers, _ROLE_HEADER) or "").strip().lower()
    subject = (_header(headers, _USER_HEADER) or "").strip()
    mfa = _mfa_satisfied(_header(headers, _MFA_HEADER))
    # The upstream forwards identity headers ONLY for a validated session, so the
    # presence of a subject or role is our authentication signal.
    authenticated = bool(subject or role)
    return Principal(subject=subject, role=role, mfa=mfa, authenticated=authenticated)


def enforce_access(principal: Optional[Principal], *, roles: Optional[Iterable[str]] = None) -> None:
    """Fail-closed RBAC + MFA gate for the assistant surface.

    Raises :class:`AssistantAccessError` unless the principal is authenticated,
    holds an authorized role, AND has satisfied a second factor. This is the
    single choke point the view and service layer call before any retrieval or
    connector egress can happen."""
    allowed = frozenset(r.lower() for r in roles) if roles is not None else authorized_roles()
    if principal is None or not principal.authenticated:
        raise AssistantAccessError("no authenticated session (authz route did not forward an identity)")
    if principal.role not in allowed:
        raise AssistantAccessError(
            f"role {principal.role or '<none>'!r} is not authorized for the assistant surface"
        )
    if not principal.mfa:
        raise AssistantAccessError("second factor (MFA) required for the assistant surface")


# ---------------------------------------------------------------------------
# PART B — egress input handling (malware / prompt-injection / minimization)
# ---------------------------------------------------------------------------

# --- malware signatures -----------------------------------------------------

# The canonical EICAR anti-malware test string (used by the acceptance test — a
# real scanner also flags it, so it is the safe synthetic "known-bad" payload).
EICAR = (
    "X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
)

# High-signal markers of executable / active content surfacing inside extracted
# document text. Kept conservative to avoid false positives on ordinary prose.
_MALWARE_MARKERS: tuple[tuple[str, str], ...] = (
    ("eicar-test-file", EICAR),
    ("pe-executable-magic", "MZ\x90\x00"),
    ("elf-executable-magic", "\x7fELF"),
    ("macho-executable-magic", "\xcf\xfa\xed\xfe"),
    ("script-tag", "<script"),
    ("powershell-encoded", "powershell -enc"),
    ("powershell-encoded", "powershell -encodedcommand"),
    ("cmd-shell", "cmd.exe /c"),
    ("vba-autoexec", "auto_open"),
    ("vba-shell", "shell(\"cmd"),
    ("wscript-shell", "wscript.shell"),
    ("base64-eval", "eval(base64"),
)


def scan_for_malware(text: str) -> list[str]:
    """Return the list of malware signatures matched in ``text`` (empty = clean).

    Signature-based, offline, deterministic. When ``ASSISTANT_MALWARE_CMD`` is
    configured this stays the first line of defence; an external scanner can be
    layered by the deploy (not wired here to keep the module dependency-free)."""
    if not text:
        return []
    haystack = text.lower()
    hits: list[str] = []
    for label, needle in _MALWARE_MARKERS:
        if needle.lower() in haystack and label not in hits:
            hits.append(label)
    return hits


# --- prompt-injection signatures --------------------------------------------

# Patterns that indicate document text is trying to act as *instructions* rather
# than DATA — i.e. to override the system prompt or exfiltrate other context.
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("override-instructions", re.compile(r"\b(ignore|disregard|forget|override)\b[^\n]*\b(previous|prior|above|earlier|all|system)\b[^\n]*\b(instruction|instructions|prompt|rule|rules|context)\b", re.I)),
    ("reveal-system-prompt", re.compile(r"\b(reveal|print|show|repeat|output|leak)\b[^\n]*\b(system|initial|hidden|your)\b[^\n]*\b(prompt|instruction|instructions|rules)\b", re.I)),
    ("role-reassignment", re.compile(r"\byou are (now|no longer)\b", re.I)),
    ("new-instructions", re.compile(r"\bnew\s+(instruction|instructions|task|directive|system prompt)\b", re.I)),
    ("role-label-spoof", re.compile(r"^\s*(system|assistant|developer)\s*:", re.I | re.M)),
    ("exfiltration", re.compile(r"\b(exfiltrate|send|email|post|upload|transmit)\b[^\n]*\b(all|other|every|each)\b[^\n]*\b(client|clients|task|tasks|document|documents|context|data)\b", re.I)),
    ("cross-context-request", re.compile(r"\b(other|another|every|all)\b[^\n]*\bclient(s)?['’]?s?\b[^\n]*\b(document|documents|data|file|files|record|records)\b", re.I)),
)

# Placeholder a defanged injection span is replaced with. It is inert prose, so
# the model reads a neutral note instead of an executable instruction.
_INJECTION_REDACTION = "[redacted: possible prompt-injection]"


def detect_prompt_injection(text: str) -> list[str]:
    """Return the list of prompt-injection signatures matched (empty = clean)."""
    if not text:
        return []
    hits: list[str] = []
    for label, pattern in _INJECTION_PATTERNS:
        if pattern.search(text) and label not in hits:
            hits.append(label)
    return hits


def defang_injection(text: str) -> tuple[str, list[str]]:
    """Neutralize injection spans in place, preserving surrounding benign text.

    Returns ``(defanged_text, matched_labels)``. When nothing matches the text is
    returned unchanged (identity), so benign chunks are byte-for-byte preserved."""
    if not text:
        return text, []
    hits: list[str] = []
    out = text
    for label, pattern in _INJECTION_PATTERNS:
        if pattern.search(out):
            hits.append(label)
            out = pattern.sub(_INJECTION_REDACTION, out)
    return out, hits


# --- data minimization (direct-identifier stripping) ------------------------

_IDENTIFIER_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    # order matters: SSN / card before the generic long-number fallbacks
    ("email", "[redacted-email]", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("ssn", "[redacted-ssn]", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card", "[redacted-card]", re.compile(r"\b(?:\d[ \-]?){13,16}\b")),
    ("phone", "[redacted-phone]", re.compile(r"(?<!\d)(?:\+?\d{1,2}[ \-.]?)?(?:\(\d{3}\)|\d{3})[ \-.]\d{3}[ \-.]\d{4}(?!\d)")),
)


def minimize_identifiers(text: str) -> tuple[str, list[str]]:
    """Strip obvious direct identifiers, returning ``(text, kinds_redacted)``.

    Benign text with no identifiers is returned unchanged."""
    if not text:
        return text, []
    kinds: list[str] = []
    out = text
    for kind, replacement, pattern in _IDENTIFIER_PATTERNS:
        new = pattern.sub(replacement, out)
        if new != out and kind not in kinds:
            kinds.append(kind)
        out = new
    return out, kinds


# --- combined hardening -----------------------------------------------------


@dataclass(frozen=True)
class GuardPolicy:
    """Toggles for the egress guard. All ON by default; configurable so the
    deploy can tune behaviour without a code change (still behind the surface's
    OFF-by-default feature flags)."""

    scan_malware: bool = True
    guard_injection: bool = True
    minimize: bool = True

    @classmethod
    def from_env(cls) -> "GuardPolicy":
        def flag(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            scan_malware=flag("ASSISTANT_EGRESS_SCAN_MALWARE", True),
            guard_injection=flag("ASSISTANT_EGRESS_GUARD_INJECTION", True),
            minimize=flag("ASSISTANT_EGRESS_MINIMIZE", True),
        )


@dataclass(frozen=True)
class HardenedChunk:
    """One chunk after hardening. ``quarantined`` chunks are dropped from egress."""

    citation: str
    text: str
    quarantined: bool = False
    malware: tuple[str, ...] = ()
    injection: tuple[str, ...] = ()
    redactions: tuple[str, ...] = ()


@dataclass
class EgressReport:
    """Aggregate evidence of what the guard did to the egress payload. Surfaced
    on the assistant result so the UI/tests can prove the acceptance criteria."""

    total: int = 0
    quarantined: list[str] = field(default_factory=list)  # citations dropped (malware)
    injection_flagged: list[str] = field(default_factory=list)  # citations defanged
    redacted: list[str] = field(default_factory=list)  # citations with stripped identifiers
    redaction_kinds: list[str] = field(default_factory=list)

    @property
    def egressed(self) -> int:
        return self.total - len(self.quarantined)

    def summary(self) -> str:
        return (
            f"{self.egressed}/{self.total} chunk(s) egressed · "
            f"{len(self.quarantined)} quarantined (malware) · "
            f"{len(self.injection_flagged)} injection-defanged · "
            f"{len(self.redacted)} identifier-redacted"
        )


def harden_chunk(citation: str, text: str, policy: Optional[GuardPolicy] = None) -> HardenedChunk:
    """Run one chunk through malware -> injection -> minimization."""
    policy = policy or GuardPolicy()
    if policy.scan_malware:
        malware = scan_for_malware(text)
        if malware:
            # Fail closed: a malicious chunk is quarantined, never egressed.
            return HardenedChunk(
                citation=citation, text="", quarantined=True, malware=tuple(malware)
            )
    else:
        malware = []

    out = text
    injection: list[str] = []
    if policy.guard_injection:
        out, injection = defang_injection(out)

    redactions: list[str] = []
    if policy.minimize:
        out, redactions = minimize_identifiers(out)

    return HardenedChunk(
        citation=citation,
        text=out,
        quarantined=False,
        malware=tuple(malware),
        injection=tuple(injection),
        redactions=tuple(redactions),
    )


def harden_context(
    sources: Sequence,
    policy: Optional[GuardPolicy] = None,
) -> tuple[list[str], EgressReport]:
    """Turn provenance-tagged sources into hardened, minimized context strings.

    ``sources`` are :class:`~review_engine.app.cross_task.CrossTaskSource` (or any
    object exposing ``.citation`` and ``.text``). Returns the context strings that
    are safe to egress (quarantined chunks excluded) plus an :class:`EgressReport`.

    Context-string format is unchanged from the pre-hardening path
    (``"[<citation>] <text>"``) so benign content is byte-identical."""
    policy = policy or GuardPolicy()
    report = EgressReport(total=len(sources))
    context: list[str] = []
    for source in sources:
        citation = getattr(source, "citation", "") or ""
        text = getattr(source, "text", "") or ""
        hardened = harden_chunk(citation, text, policy)
        if hardened.quarantined:
            report.quarantined.append(citation)
            continue
        if hardened.injection:
            report.injection_flagged.append(citation)
        if hardened.redactions:
            report.redacted.append(citation)
            for kind in hardened.redactions:
                if kind not in report.redaction_kinds:
                    report.redaction_kinds.append(kind)
        context.append(f"[{citation}] {hardened.text}")
    return context, report
