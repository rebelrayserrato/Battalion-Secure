"""Controlled US-state jurisdiction list for the Client concept (RAYAAAA-244).

Jurisdiction is a *validated* value drawn from this controlled list, never free
text. Phase B/C (policy library + law grounding) will scope corpora/retrieval to
this same code, so keeping it canonical (a stable 2-letter code) here is the
foundation the later phases depend on.

``UNSPECIFIED_STATE`` ("US") is an explicit, validated member of the list meaning
"unspecified / federal". Jurisdiction was optional free-text before this change,
so a client whose state is genuinely unknown needs a canonical value rather than a
NULL; "US" fills that role without smuggling free text back in. The migration
(RAYAAAA-244) parks pre-existing matters with no recognizable jurisdiction under a
default client whose state is ``UNSPECIFIED_STATE`` and preserves the original
free-text string in the audit log so nothing is lost.
"""

from __future__ import annotations

# Canonical code used when a client's jurisdiction is unspecified / federal.
UNSPECIFIED_STATE = "US"

# 50 states + DC. Code -> display name. Kept in a single authoritative dict so the
# UI picker, validation, and the migration all agree on exactly one list.
STATE_NAMES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

# The full validated set, including the "unspecified/federal" sentinel.
_LABELS: dict[str, str] = {UNSPECIFIED_STATE: "US — Unspecified / federal", **STATE_NAMES}

# Reverse lookup (lowercased display name -> code) so a legacy free-text value
# like "california" or "New York" also normalizes cleanly during migration.
_NAME_TO_CODE: dict[str, str] = {name.lower(): code for code, name in STATE_NAMES.items()}

# Order presented in the UI picker: the sentinel first (default), then states A-Z.
JURISDICTION_CHOICES: list[str] = [UNSPECIFIED_STATE] + sorted(STATE_NAMES)


def state_label(code: str) -> str:
    """Human label for a code, e.g. ``"CA" -> "California"``. Unknown codes echo."""
    return _LABELS.get((code or "").strip().upper(), code)


def normalize_state(value: str | None) -> str | None:
    """Return the canonical code for ``value`` or ``None`` if unrecognized.

    Accepts a 2-letter code (any case) or a full state name (any case). Empty /
    unknown input returns ``None`` so callers can decide the fallback; use
    :func:`validate_state` when a value is required.
    """
    if value is None:
        return None
    token = value.strip()
    if not token:
        return None
    upper = token.upper()
    if upper in _LABELS:
        return upper
    return _NAME_TO_CODE.get(token.lower())


def is_valid_state(value: str | None) -> bool:
    return normalize_state(value) is not None


def validate_state(value: str | None) -> str:
    """Return the canonical code or raise ``ValueError``. Use where a client MUST
    carry a jurisdiction (create/update client)."""
    code = normalize_state(value)
    if code is None:
        raise ValueError(
            f"Unknown jurisdiction {value!r}; expected a US state code/name "
            f"or {UNSPECIFIED_STATE!r} for unspecified."
        )
    return code
