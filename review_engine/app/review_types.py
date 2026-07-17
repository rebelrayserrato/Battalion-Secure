"""Canonical review-type catalogue (RAYAAAA-263 / owner base44 demo).

The Dashboard "Start a Review" cards and the New Request wizard (sibling
RAYAAAA issue) share this single list so a card can prefilter the wizard by
``key``. Each entry carries the demo's title/subtitle plus a coloured icon chip
(emoji + hex) used by the dashboard cards. Kept UI-agnostic (no Streamlit import)
so both surfaces — and tests — can import it.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewType:
    key: str
    title: str
    subtitle: str
    icon: str
    color: str


# Order + copy mirror the owner's demo Dashboard "Start a Review" grid. Keys are
# kept IDENTICAL to the New Request wizard's presets (sibling RAYAAAA-264,
# ``review_engine/app/new_request.py``) so a dashboard card can stash its key in
# ``st.session_state['nr_type']`` and land the wizard prefiltered to that type.
REVIEW_TYPES: list[ReviewType] = [
    ReviewType("legal_case", "Legal Case Analysis", "Case file review", "⚖️", "#3b82f6"),
    ReviewType("hr_termination", "HR & Termination Review", "Termination letter compliance", "\U0001f465", "#f59e0b"),
    ReviewType("contract", "Contract Review", "Vendor contracts", "\U0001f4dd", "#8b5cf6"),
    ReviewType("compliance_audit", "Compliance Audit", "HIPAA compliance", "✅", "#10b981"),
    ReviewType("incident_misconduct", "Incident & Misconduct Review", "Workplace incidents", "❗", "#ef4444"),
    ReviewType("general_document", "General Document Review", "Policy Q&A", "\U0001f4c4", "#64748b"),
]

REVIEW_TYPES_BY_KEY: dict[str, ReviewType] = {rt.key: rt for rt in REVIEW_TYPES}
