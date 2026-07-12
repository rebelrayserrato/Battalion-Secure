# Reviewer decision workspace (RAYAAAA-238, Phase 3a)

A human reviewer triages a Task's source chunks in the Streamlit app's **Review**
tab: each chunk (SRC ID) can be marked **approved / rejected / needs_changes**
with an optional free-text note. Decisions persist on disk per-Task so they
survive a reload or app restart, and the Phase 3b branded report generator
(RAYAAAA-241, LeadEng) consumes them.

Synthetic / local data only — no external calls, no real client PII (Phase 4
gate stands).

## Storage location

Per-Task file: `review_engine/data/reviewer_decisions/<task_id>.json`
(computed by `decisions.decisions_path(task_id)`).

This is deliberately the same path the P3b report generator reads from
(`review_engine/reports/decisions.py::default_decisions_path`), so the reviewer
workspace and the report generator agree with no extra wiring.

## Interface contract for P3b (do not change without syncing on RAYAAAA-235)

```json
{
  "task_id": "MAT-XXXXXXXXXX",
  "decisions": {
    "SRC-1A2B3C4D5E6F": {
      "status": "approved | rejected | needs_changes | undecided",
      "note": "free-text reviewer note (\"\" if none)",
      "decided_at": "2026-07-12T22:39:25+00:00",
      "reviewer": "reviewer identity (\"\" if unknown)"
    }
  }
}
```

- Keys under `decisions` are SRC chunk ids (`source_ref`, e.g. `SRC-...`).
- Only SRC IDs the reviewer has touched are stored. Any SRC ID **absent** from
  the map is `undecided` with an empty note.
- `decided_at` is a UTC ISO-8601 timestamp refreshed only when the status/note
  actually changes.

## Reading decisions from P3b

```python
from review_engine.reviewer import decisions

store = decisions.load_decisions(task_id)          # schema-shaped, tolerant of missing/corrupt file
entry = decisions.get_decision(store, src_id)      # -> {status, note, decided_at, reviewer}, defaults to undecided
counts = decisions.summary_counts(store, src_ids)  # {approved, rejected, needs_changes, undecided, total, decided}
```

`load_decisions` never raises for I/O or JSON errors, so it is safe to call
offline and for Tasks that have no decisions yet.

If the schema must change, comment on **RAYAAAA-235** so LeadEng (P3b) can sync.
