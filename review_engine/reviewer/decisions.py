"""Persistence for reviewer triage decisions (RAYAAAA-238, Phase 3a).

A human reviewer marks each source chunk (SRC ID) for a Task as
approved / rejected / needs_changes and can attach a free-text note. These
decisions are stored per-Task on disk so they survive a page reload or an app
restart, and so the Phase 3b report generator can consume them.

Interface contract for P3b (RAYAAAA-241) — persisted JSON schema::

    {
        "task_id": <str>,                       # the Task / matter id
        "decisions": {
            <SRC_ID>: {                          # e.g. "SRC-1A2B3C4D5E6F"
                "status": "approved" | "rejected" | "needs_changes" | "undecided",
                "note": <str>,                   # free-text reviewer note ("" if none)
                "decided_at": <iso8601 str|None>,# UTC timestamp of the last change
                "reviewer": <str>                # reviewer identity ("" if unknown)
            },
            ...
        }
    }

Only SRC IDs the reviewer has touched are stored. Any SRC ID absent from the
map is treated as ``undecided`` with an empty note (see :func:`get_decision`).
If the schema must change, comment on RAYAAAA-235 so LeadEng (P3b) can sync.

Decisions are stored at ``<data dir>/reviewer_decisions/<task_id>.json`` — the
same location the P3b report generator reads from — so the two integrate with
no extra configuration.

Everything here is local, offline-safe, and never makes an external call.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional

from review_engine.config.settings import DATA_DIR

# Per-Task decisions live at ``<data dir>/reviewer_decisions/<task_id>.json``.
# This is the exact location the P3b report generator reads from
# (``review_engine/reports/decisions.py::default_decisions_path``), so the
# workspace and the report generator agree with no extra wiring.
DECISIONS_SUBDIR = "reviewer_decisions"

VALID_STATUSES = ("approved", "rejected", "needs_changes", "undecided")
DEFAULT_STATUS = "undecided"

# Statuses that count as a real reviewer decision (i.e. not "undecided").
_DECIDED_STATUSES = tuple(s for s in VALID_STATUSES if s != DEFAULT_STATUS)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def decisions_dir(base: Optional[Path] = None) -> Path:
    """Return the directory holding per-Task reviewer-decision files."""
    root = Path(base) if base is not None else DATA_DIR
    return root / DECISIONS_SUBDIR


def decisions_path(matter_id: str, base: Optional[Path] = None) -> Path:
    """Return the path to a Task's ``<task_id>.json`` decisions file."""
    return decisions_dir(base) / f"{matter_id}.json"


def empty_store(matter_id: str) -> dict:
    """Return an empty, schema-shaped decisions store for ``matter_id``."""
    return {"task_id": matter_id, "decisions": {}}


def _normalize_entry(raw: object) -> dict:
    """Coerce a persisted/inbound entry into the documented shape."""
    entry = raw if isinstance(raw, Mapping) else {}
    status = entry.get("status", DEFAULT_STATUS)
    if status not in VALID_STATUSES:
        status = DEFAULT_STATUS
    note = entry.get("note", "")
    return {
        "status": status,
        "note": "" if note is None else str(note),
        "decided_at": entry.get("decided_at"),
        "reviewer": str(entry.get("reviewer") or ""),
    }


def load_decisions(matter_id: str, base: Optional[Path] = None) -> dict:
    """Load a Task's decisions, tolerating a missing or corrupt file.

    Always returns a schema-shaped dict; never raises for I/O or JSON errors so
    the UI stays offline-safe.
    """
    store = empty_store(matter_id)
    path = decisions_path(matter_id, base)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return store
    if not isinstance(raw, Mapping):
        return store
    store["task_id"] = str(raw.get("task_id") or matter_id)
    decisions = raw.get("decisions")
    if isinstance(decisions, Mapping):
        store["decisions"] = {
            str(src_id): _normalize_entry(entry) for src_id, entry in decisions.items()
        }
    return store


def _write_store(matter_id: str, store: dict, base: Optional[Path]) -> Path:
    """Atomically write ``store`` to the Task's decisions file."""
    directory = decisions_dir(base)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{matter_id}.json"
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=".reviewer_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(store, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path


def get_decision(store: dict, src_id: str) -> dict:
    """Return the decision entry for ``src_id`` or an undecided default."""
    decisions = store.get("decisions", {}) if isinstance(store, Mapping) else {}
    entry = decisions.get(src_id)
    if entry is None:
        return {"status": DEFAULT_STATUS, "note": "", "decided_at": None, "reviewer": ""}
    return _normalize_entry(entry)


def record_decision(
    matter_id: str,
    src_id: str,
    status: str,
    note: str = "",
    reviewer: str = "",
    base: Optional[Path] = None,
    now: Optional[str] = None,
) -> dict:
    """Persist a single reviewer decision and return the updated store."""
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status {status!r}; expected one of {VALID_STATUSES}")
    store = load_decisions(matter_id, base)
    store["decisions"][src_id] = {
        "status": status,
        "note": "" if note is None else str(note),
        "decided_at": now or _now_iso(),
        "reviewer": str(reviewer or ""),
    }
    _write_store(matter_id, store, base)
    return store


def save_decisions(
    matter_id: str,
    decisions: Mapping[str, Mapping[str, object]],
    reviewer: str = "",
    base: Optional[Path] = None,
    now: Optional[str] = None,
) -> dict:
    """Persist a batch of decisions (e.g. a whole review form submission).

    ``decisions`` maps SRC ID -> ``{"status": ..., "note": ...}``. The
    ``decided_at`` timestamp is refreshed only for entries whose status or note
    actually changed relative to what is already on disk, so an unchanged row's
    original decision time is preserved.
    """
    store = load_decisions(matter_id, base)
    stamp = now or _now_iso()
    for src_id, incoming in decisions.items():
        incoming = incoming if isinstance(incoming, Mapping) else {}
        status = incoming.get("status", DEFAULT_STATUS)
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status {status!r} for {src_id!r}")
        note = incoming.get("note", "")
        note = "" if note is None else str(note)
        previous = store["decisions"].get(str(src_id))
        changed = (
            previous is None
            or previous.get("status") != status
            or previous.get("note", "") != note
        )
        decided_at = stamp if changed else previous.get("decided_at")
        store["decisions"][str(src_id)] = {
            "status": status,
            "note": note,
            "decided_at": decided_at,
            "reviewer": str(reviewer or "") if changed else previous.get("reviewer", ""),
        }
    _write_store(matter_id, store, base)
    return store


def summary_counts(
    store: dict,
    src_ids: Optional[Iterable[str]] = None,
) -> dict:
    """Count decisions by status for a Task.

    When ``src_ids`` (the full universe of reviewable SRC IDs for the Task) is
    given, any SRC not decided — or explicitly ``undecided`` — is counted as
    ``undecided`` and ``total`` reflects that universe. When it is omitted, only
    stored decisions are counted.
    """
    counts = {status: 0 for status in VALID_STATUSES}
    decisions = store.get("decisions", {}) if isinstance(store, Mapping) else {}

    if src_ids is not None:
        universe = list(dict.fromkeys(str(s) for s in src_ids))
        for src in universe:
            status = get_decision(store, src)["status"]
            counts[status] += 1
        total = len(universe)
    else:
        for entry in decisions.values():
            status = _normalize_entry(entry)["status"]
            counts[status] += 1
        total = sum(counts.values())

    counts["total"] = total
    counts["decided"] = sum(counts[s] for s in _DECIDED_STATUSES)
    return counts
