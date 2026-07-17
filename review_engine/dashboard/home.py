"""Pure aggregation for the Dashboard landing view (RAYAAAA-263).

The owner (RAYAAAA-191) asked the Review Engine to open on a "Welcome back"
dashboard modelled on their base44 demo: four stat tiles (Total Requests /
In Progress / Completed / Needs Review) wired to real Task/review data, plus a
Recent Requests panel.

This module holds ONLY the pure data derivation so it is unit-testable without a
running Streamlit session (:mod:`review_engine.app.dashboard_home` renders it).
A "Request" is a Task/matter; its status is derived from the same
documents / findings / reviewer-decision data the rest of the app already
produces — no new state is stored and nothing here mutates the database.
"""
from __future__ import annotations

from typing import Any

from review_engine.reviewer import decisions as reviewer_decisions

# The four dashboard buckets. Total spans every Task; the other three partition
# the same set (each Task is counted in exactly one), so they always sum to
# ``total`` and the tiles reconcile.
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_NEEDS_REVIEW = "needs_review"


def matter_status(db: Any, matter: dict) -> str:
    """Derive a single lifecycle status for one Task from existing review data.

    Heuristic (documented so the tiles are explainable), in priority order:

    * ``completed`` — a human reviewer has recorded decisions and nothing is left
      ``undecided`` or ``needs_changes`` (the review is signed off).
    * ``needs_review`` — the pipeline has produced findings but the human review
      is not finished.
    * ``in_progress`` — everything else (freshly created, uploading, processing,
      or reviewed-but-no-findings-yet).
    """
    matter_id = matter["id"]
    store = reviewer_decisions.load_decisions(matter_id)
    counts = reviewer_decisions.summary_counts(store)
    findings = db.get_findings(matter_id)

    has_decisions = counts["total"] > 0
    review_settled = counts["undecided"] == 0 and counts["needs_changes"] == 0
    if has_decisions and review_settled:
        return STATUS_COMPLETED
    if findings:
        return STATUS_NEEDS_REVIEW
    return STATUS_IN_PROGRESS


def dashboard_stats(db: Any) -> dict[str, int]:
    """Return the four tile counts as ``{total, in_progress, completed, needs_review}``."""
    matters = db.list_matters()
    buckets = {
        "total": len(matters),
        STATUS_IN_PROGRESS: 0,
        STATUS_COMPLETED: 0,
        STATUS_NEEDS_REVIEW: 0,
    }
    for matter in matters:
        buckets[matter_status(db, matter)] += 1
    return buckets


def recent_requests(db: Any, limit: int = 5) -> list[dict]:
    """Return the most-recent Tasks (already ``created_at DESC``) with status."""
    rows = []
    for matter in db.list_matters()[: max(0, limit)]:
        rows.append(
            {
                "id": matter["id"],
                "name": matter["name"],
                "client_name": matter.get("client_name") or "—",
                "status": matter_status(db, matter),
            }
        )
    return rows
