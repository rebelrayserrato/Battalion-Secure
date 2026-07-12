"""Unit coverage for reviewer decision persistence (RAYAAAA-238, Phase 3a)."""

from __future__ import annotations

import json

import pytest

from review_engine.reviewer import decisions as d


def test_load_missing_returns_empty_store(tmp_path):
    store = d.load_decisions("MAT-EMPTY", base=tmp_path)
    assert store == {"task_id": "MAT-EMPTY", "decisions": {}}


def test_record_and_reload_roundtrip(tmp_path):
    d.record_decision(
        "MAT-1", "SRC-AAA", "approved", note="looks good", reviewer="alice", base=tmp_path
    )
    # Simulate an app restart: a brand new read from disk.
    reloaded = d.load_decisions("MAT-1", base=tmp_path)
    entry = reloaded["decisions"]["SRC-AAA"]
    assert entry["status"] == "approved"
    assert entry["note"] == "looks good"
    assert entry["reviewer"] == "alice"
    assert entry["decided_at"]  # iso8601 timestamp present


def test_decisions_persist_on_disk(tmp_path):
    d.record_decision("MAT-1", "SRC-AAA", "rejected", base=tmp_path)
    path = d.decisions_path("MAT-1", base=tmp_path)
    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["task_id"] == "MAT-1"
    assert raw["decisions"]["SRC-AAA"]["status"] == "rejected"


def test_record_invalid_status_raises(tmp_path):
    with pytest.raises(ValueError):
        d.record_decision("MAT-1", "SRC-AAA", "maybe", base=tmp_path)


def test_save_decisions_batch(tmp_path):
    d.save_decisions(
        "MAT-2",
        {
            "SRC-A": {"status": "approved", "note": ""},
            "SRC-B": {"status": "needs_changes", "note": "clarify date"},
        },
        reviewer="bob",
        base=tmp_path,
    )
    store = d.load_decisions("MAT-2", base=tmp_path)
    assert store["decisions"]["SRC-A"]["status"] == "approved"
    assert store["decisions"]["SRC-B"]["note"] == "clarify date"
    assert store["decisions"]["SRC-B"]["reviewer"] == "bob"


def test_save_decisions_invalid_status_raises(tmp_path):
    with pytest.raises(ValueError):
        d.save_decisions("MAT-2", {"SRC-A": {"status": "nope"}}, base=tmp_path)


def test_decided_at_preserved_when_unchanged(tmp_path):
    d.save_decisions(
        "MAT-3", {"SRC-A": {"status": "approved", "note": "n"}}, base=tmp_path, now="2020-01-01T00:00:00+00:00"
    )
    # Re-save the identical decision at a later timestamp; decided_at must not move.
    d.save_decisions(
        "MAT-3", {"SRC-A": {"status": "approved", "note": "n"}}, base=tmp_path, now="2099-01-01T00:00:00+00:00"
    )
    store = d.load_decisions("MAT-3", base=tmp_path)
    assert store["decisions"]["SRC-A"]["decided_at"] == "2020-01-01T00:00:00+00:00"


def test_decided_at_refreshed_when_changed(tmp_path):
    d.save_decisions(
        "MAT-4", {"SRC-A": {"status": "approved", "note": "n"}}, base=tmp_path, now="2020-01-01T00:00:00+00:00"
    )
    d.save_decisions(
        "MAT-4", {"SRC-A": {"status": "rejected", "note": "n"}}, base=tmp_path, now="2099-01-01T00:00:00+00:00"
    )
    store = d.load_decisions("MAT-4", base=tmp_path)
    assert store["decisions"]["SRC-A"]["decided_at"] == "2099-01-01T00:00:00+00:00"


def test_get_decision_default_for_unknown_src(tmp_path):
    store = d.load_decisions("MAT-1", base=tmp_path)
    entry = d.get_decision(store, "SRC-UNKNOWN")
    assert entry == {"status": "undecided", "note": "", "decided_at": None, "reviewer": ""}


def test_corrupt_file_is_tolerated(tmp_path):
    path = d.decisions_path("MAT-BAD", base=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not valid json", encoding="utf-8")
    store = d.load_decisions("MAT-BAD", base=tmp_path)
    assert store == {"task_id": "MAT-BAD", "decisions": {}}


def test_summary_counts_with_universe(tmp_path):
    d.save_decisions(
        "MAT-5",
        {
            "SRC-A": {"status": "approved"},
            "SRC-B": {"status": "rejected"},
            "SRC-C": {"status": "needs_changes"},
        },
        base=tmp_path,
    )
    store = d.load_decisions("MAT-5", base=tmp_path)
    counts = d.summary_counts(store, ["SRC-A", "SRC-B", "SRC-C", "SRC-D", "SRC-E"])
    assert counts["approved"] == 1
    assert counts["rejected"] == 1
    assert counts["needs_changes"] == 1
    assert counts["undecided"] == 2  # SRC-D and SRC-E never decided
    assert counts["total"] == 5
    assert counts["decided"] == 3


def test_summary_counts_without_universe(tmp_path):
    d.save_decisions(
        "MAT-6",
        {"SRC-A": {"status": "approved"}, "SRC-B": {"status": "approved"}},
        base=tmp_path,
    )
    store = d.load_decisions("MAT-6", base=tmp_path)
    counts = d.summary_counts(store)
    assert counts["approved"] == 2
    assert counts["total"] == 2
    assert counts["undecided"] == 0


def test_summary_counts_empty_task():
    counts = d.summary_counts(d.empty_store("MAT-Z"), [])
    assert counts["total"] == 0
    assert counts["decided"] == 0
    assert all(counts[s] == 0 for s in d.VALID_STATUSES)
