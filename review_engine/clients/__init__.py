"""Client concept for the Battalion-Secure review engine (RAYAAAA-244, Phase A).

A ``Client`` is the first-class owner of one or more Tasks/matters. Jurisdiction
(US state) lives on the Client, not free-text per matter, so a Task's jurisdiction
can never diverge from its client's.

The Client *identity* deliberately reuses the GDPR erasure fan-out's client->matter
grouping rather than inventing a second identity store — see
``docs/RAYAAAA-244-client-concept.md``.
"""

from review_engine.clients.jurisdictions import (
    JURISDICTION_CHOICES,
    STATE_NAMES,
    UNSPECIFIED_STATE,
    is_valid_state,
    normalize_state,
    state_label,
)

__all__ = [
    "JURISDICTION_CHOICES",
    "STATE_NAMES",
    "UNSPECIFIED_STATE",
    "is_valid_state",
    "normalize_state",
    "state_label",
]
