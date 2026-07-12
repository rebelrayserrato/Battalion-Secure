"""Reviewer-decisions contract (P3a / RAYAAAA-238 boundary).

The report generator (P3b / RAYAAAA-239) consumes reviewer decisions produced by
the reviewer-workspace (P3a / RAYAAAA-238) at this schema boundary ONLY:

    {
      "task_id": "<matter_id>",
      "decisions": {
        "<SRC_ID>": {
          "status": "approve" | "reject" | "needs-changes",
          "note": "<free text>",
          "decided_at": "<ISO-8601 timestamp>",
          "reviewer": "<name>"
        },
        ...
      }
    }

Decisions are keyed by SRC ID (the ``source_ref`` on a finding's supporting
sources). This module owns loading/normalisation so the generator stays decoupled
from how P3a writes the file. If this schema changes, coordinate on RAYAAAA-235.

SYNTHETIC/local data only — no real client PII (Phase 4 gate stands).
"""

from __future__ import annotations

import json
from pathlib import Path

# Canonical decision statuses used throughout the report.
APPROVED = "approved"
REJECTED = "rejected"
NEEDS_CHANGES = "needs-changes"
UNDECIDED = "undecided"

# Accept the P3a wire values plus common variants, map to canonical form.
_STATUS_ALIASES = {
    "approve": APPROVED,
    "approved": APPROVED,
    "reject": REJECTED,
    "rejected": REJECTED,
    "needs-changes": NEEDS_CHANGES,
    "needs_changes": NEEDS_CHANGES,
    "needs changes": NEEDS_CHANGES,
    "changes": NEEDS_CHANGES,
}


def normalize_status(status: str | None) -> str:
    """Map a raw P3a status onto a canonical label; unknown/empty → undecided."""
    if not status:
        return UNDECIDED
    return _STATUS_ALIASES.get(str(status).strip().lower(), UNDECIDED)


def default_decisions_path(db_path: str | Path, matter_id: str) -> Path:
    """Conventional location a P3a reviewer-decisions file for ``matter_id``.

    Lives beside the review database so the workspace and report generator agree
    without extra configuration: ``<db dir>/reviewer_decisions/<matter_id>.json``.
    """
    return Path(db_path).parent / "reviewer_decisions" / f"{matter_id}.json"


def load_decisions(source, matter_id: str | None = None) -> dict:
    """Load and normalise reviewer decisions from a dict, path, or None.

    Degrades gracefully (returns ``{}``) when ``source`` is None, the path does
    not exist, or the file is malformed — the report renders without a reviewer
    section rather than crashing (AC3).

    Returns a mapping ``{SRC_ID: {"status", "note", "decided_at", "reviewer"}}``
    with canonicalised statuses.
    """
    if source is None:
        return {}
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.exists():
            return {}
        try:
            source = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    if not isinstance(source, dict):
        return {}

    # Accept either the full contract envelope or a bare decisions mapping.
    raw = source.get("decisions", source)
    if not isinstance(raw, dict):
        return {}
    if matter_id is not None and "task_id" in source and source["task_id"] != matter_id:
        # Decisions file is for a different Task; ignore rather than mislabel.
        return {}

    decisions: dict[str, dict] = {}
    for src_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        decisions[str(src_id)] = {
            "status": normalize_status(entry.get("status")),
            "note": entry.get("note") or "",
            "decided_at": entry.get("decided_at") or "",
            "reviewer": entry.get("reviewer") or "",
        }
    return decisions


def finding_decision(finding: dict, decisions: dict) -> tuple[str | None, dict | None]:
    """Return ``(src_id, decision)`` for the first supporting source that has a
    decision, else ``(None, None)``."""
    for source in finding.get("supporting_sources", []):
        src_id = source.get("source_ref")
        if src_id in decisions:
            return src_id, decisions[src_id]
    return None, None


def rollup(findings: list, decisions: dict) -> dict:
    """Count findings by their reviewer-decision status (AC5 rollup)."""
    counts = {APPROVED: 0, REJECTED: 0, NEEDS_CHANGES: 0, UNDECIDED: 0}
    for finding in findings:
        _, decision = finding_decision(finding, decisions)
        status = decision["status"] if decision else UNDECIDED
        counts[status] = counts.get(status, 0) + 1
    return counts
