"""Tests for the Dashboard landing aggregation (RAYAAAA-263).

Covers the pure status derivation + tile counts in
:mod:`review_engine.dashboard.home` and the shared review-type catalogue. No
Streamlit session is needed — the rendering module is a thin wrapper over these.
"""
from review_engine.app.review_types import REVIEW_TYPES, REVIEW_TYPES_BY_KEY
from review_engine.dashboard import home


class _FakeDB:
    def __init__(self, matters, findings=None):
        self._matters = matters
        self._findings = findings or {}

    def list_matters(self):
        return list(self._matters)

    def get_findings(self, matter_id):
        return list(self._findings.get(matter_id, []))


def _store(**by_src):
    """Build a reviewer-decisions store shaped like reviewer.decisions.load_decisions."""
    return {"task_id": "t", "decisions": {src: {"status": s} for src, s in by_src.items()}}


def _patch_decisions(monkeypatch, stores):
    monkeypatch.setattr(
        home.reviewer_decisions,
        "load_decisions",
        lambda matter_id, *a, **k: stores.get(matter_id, {"task_id": matter_id, "decisions": {}}),
    )


def test_in_progress_when_no_findings_and_no_decisions(monkeypatch):
    db = _FakeDB([{"id": "MAT-1", "name": "A"}])
    _patch_decisions(monkeypatch, {})
    assert home.matter_status(db, db.list_matters()[0]) == "in_progress"


def test_needs_review_when_findings_but_review_unsettled(monkeypatch):
    db = _FakeDB(
        [{"id": "MAT-1", "name": "A"}],
        findings={"MAT-1": [{"title": "x"}]},
    )
    _patch_decisions(monkeypatch, {"MAT-1": _store(s1="undecided")})
    assert home.matter_status(db, db.list_matters()[0]) == "needs_review"


def test_completed_when_all_decisions_settled(monkeypatch):
    db = _FakeDB(
        [{"id": "MAT-1", "name": "A"}],
        findings={"MAT-1": [{"title": "x"}]},
    )
    _patch_decisions(monkeypatch, {"MAT-1": _store(s1="approved", s2="rejected")})
    assert home.matter_status(db, db.list_matters()[0]) == "completed"


def test_needs_changes_keeps_out_of_completed(monkeypatch):
    db = _FakeDB(
        [{"id": "MAT-1", "name": "A"}],
        findings={"MAT-1": [{"title": "x"}]},
    )
    _patch_decisions(monkeypatch, {"MAT-1": _store(s1="approved", s2="needs_changes")})
    assert home.matter_status(db, db.list_matters()[0]) == "needs_review"


def test_dashboard_stats_partition_sums_to_total(monkeypatch):
    matters = [
        {"id": "MAT-1", "name": "A"},  # in_progress (nothing)
        {"id": "MAT-2", "name": "B"},  # needs_review (findings, unsettled)
        {"id": "MAT-3", "name": "C"},  # completed (settled decisions)
    ]
    db = _FakeDB(
        matters,
        findings={"MAT-2": [{"title": "x"}], "MAT-3": [{"title": "y"}]},
    )
    _patch_decisions(
        monkeypatch,
        {
            "MAT-2": _store(s1="undecided"),
            "MAT-3": _store(s1="approved"),
        },
    )
    stats = home.dashboard_stats(db)
    assert stats["total"] == 3
    assert stats["in_progress"] == 1
    assert stats["needs_review"] == 1
    assert stats["completed"] == 1
    assert stats["in_progress"] + stats["needs_review"] + stats["completed"] == stats["total"]


def test_recent_requests_carries_name_client_status(monkeypatch):
    matters = [
        {"id": "MAT-1", "name": "Alpha", "client_name": "Acme"},
        {"id": "MAT-2", "name": "Bravo", "client_name": None},
    ]
    db = _FakeDB(matters, findings={"MAT-1": [{"title": "x"}]})
    _patch_decisions(monkeypatch, {"MAT-1": _store(s1="undecided")})
    rows = home.recent_requests(db, limit=5)
    assert [r["name"] for r in rows] == ["Alpha", "Bravo"]
    assert rows[0]["client_name"] == "Acme"
    assert rows[0]["status"] == "needs_review"
    assert rows[1]["client_name"] == "—"  # None coerced to a dash


def test_recent_requests_respects_limit(monkeypatch):
    matters = [{"id": f"MAT-{i}", "name": str(i)} for i in range(10)]
    db = _FakeDB(matters)
    _patch_decisions(monkeypatch, {})
    assert len(home.recent_requests(db, limit=3)) == 3


def test_review_types_catalogue_shape():
    # The six demo review types, stable keys, and a working by-key index.
    assert len(REVIEW_TYPES) == 6
    keys = [rt.key for rt in REVIEW_TYPES]
    # Keys are kept identical to the sibling RAYAAAA-264 New Request wizard presets
    # so a dashboard card can prefilter the wizard via st.session_state["nr_type"].
    assert keys == [
        "legal_case",
        "hr_termination",
        "contract",
        "compliance_audit",
        "incident_misconduct",
        "general_document",
    ]
    assert REVIEW_TYPES_BY_KEY["contract"].title == "Contract Review"
    # every type has an icon + hex colour for its chip
    for rt in REVIEW_TYPES:
        assert rt.icon and rt.color.startswith("#")
