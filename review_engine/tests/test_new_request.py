"""RAYAAAA-264: New Request wizard presets + submission mapping."""
from __future__ import annotations

from review_engine.app import new_request as nr
from review_engine.app import review_types as rt_catalog


def test_wizard_shares_the_263_catalogue_keys_and_titles():
    # The wizard must stay key/title/icon-synced with the shared catalogue that
    # the RAYAAAA-263 Dashboard cards use, so the nr_type prefilter always lands.
    cat = {c.key: c for c in rt_catalog.REVIEW_TYPES}
    wiz = {r.key: r for r in nr.REVIEW_TYPES}
    assert list(wiz) == list(cat)  # same order + keys
    for key, r in wiz.items():
        assert r.title == cat[key].title
        assert r.icon == cat[key].icon
        assert r.accent == cat[key].color


def test_six_review_types_with_stable_keys():
    keys = [rt.key for rt in nr.REVIEW_TYPES]
    assert keys == [
        "legal_case",
        "hr_termination",
        "contract",
        "compliance_audit",
        "incident_misconduct",
        "general_document",
    ]
    # Every card has copy + at least three feature chips (base44 shows 3 + "more").
    for rt in nr.REVIEW_TYPES:
        assert rt.title and rt.description
        assert len(rt.features) >= 3


def test_review_type_lookup():
    assert nr.review_type("contract").title == "Contract Review"
    assert nr.review_type("does_not_exist") is None


def test_chips_html_shows_overflow_pill():
    rt = nr.review_type("legal_case")  # 5 features
    html = nr._chips_html(rt, shown=3)
    assert "+2 more" in html
    assert rt.features[0] in html


def test_stepper_marks_progress():
    step1 = nr._stepper_html(1)
    assert "nr-step active" in step1  # step 1 active
    step2 = nr._stepper_html(2)
    assert "nr-step-line done" in step2  # connector filled once on step 2


def test_preset_pipeline_flags_map_onto_existing_capabilities():
    # Presets only toggle existing capabilities; contract review is audit-only.
    contract = nr.review_type("contract")
    assert contract.run_policy_audit and not contract.include_hr and not contract.include_fraud
    incident = nr.review_type("incident_misconduct")
    assert incident.include_hr and incident.include_fraud
    hr = nr.review_type("hr_termination")
    assert hr.include_hr and hr.run_policy_audit and hr.law_grounded


class _FakeDB:
    def __init__(self):
        self.logged = []

    def get_matter(self, mid):
        return {"client_id": "CLI-1", "jurisdiction": "AZ"}

    def get_policy_chunks(self, cid):
        return []

    def log(self, *a):
        self.logged.append(a)


class _FakeSvc:
    def __init__(self):
        self.db = _FakeDB()
        self.calls = {}

    def process_matter(self, mid):
        self.calls["process"] = mid
        return {"processed": 1, "chunks": 3, "errors": []}

    def run_reviews(self, mid, include_hr, include_fraud):
        self.calls["run_reviews"] = (include_hr, include_fraud)
        return [{"category": "HR", "title": "t", "confidence": "low",
                 "explanation": "e", "supporting_sources": []}]


def test_run_submission_drives_existing_pipeline(monkeypatch):
    # No policy audit / no question for the general preset -> just process+review.
    rt = nr.review_type("general_document")
    svc = _FakeSvc()
    out = nr._run_submission(svc, "MAT-1", rt, question="")
    # _run_submission drives the existing pipeline (process + run_reviews), never
    # a forked backend.
    assert svc.calls["process"] == "MAT-1"
    assert svc.calls["run_reviews"] == (True, True)
    assert out["chunks"] == 3
    assert len(out["findings"]) == 1
    assert "answer" not in out  # empty question -> no grounded answer
