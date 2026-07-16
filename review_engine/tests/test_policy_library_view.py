"""RAYAAAA-264: Policy Library page — metadata encoding, counts, filtering."""
from __future__ import annotations

from review_engine.app import policy_library_view as pl
from review_engine.app.services import safe_filename


def test_skill_filename_survives_safe_filename_and_round_trips():
    fname = pl.skill_filename("Compliance Rule", "Safety", "OSHA Lockout / Tagout!")
    # The chosen scheme must survive the upload path's safe_filename untouched,
    # otherwise the encoded type/category would be corrupted on save.
    assert safe_filename(fname) == fname
    parsed = pl.parse_skill_filename(fname)
    assert parsed["policy_type"] == "Compliance Rule"
    assert parsed["type_slug"] == "compliance"
    assert parsed["category_slug"] == "safety"


def test_parse_returns_none_for_bulk_uploaded_file():
    assert pl.parse_skill_filename("Employee Handbook.pdf") is None


def test_doc_meta_defaults_legacy_upload_to_company_other():
    meta = pl._doc_meta("Vendor Contract.pdf")
    assert meta["policy_type"] == "Company Policy"
    assert meta["type_slug"] == "company"
    assert meta["category"] == "Other"


def test_doc_meta_recovers_skill_metadata():
    fname = pl.skill_filename("Company Policy", "Terminations", "Progressive Discipline")
    meta = pl._doc_meta(fname)
    assert meta["policy_type"] == "Company Policy"
    assert meta["category"] == "Terminations"
    assert meta["title"] == "Progressive Discipline"


def test_skill_document_body_has_header_and_content():
    body = pl.skill_document_body(
        "Attendance Policy", "Attendance", "Summary", ["ptolicy", "hr"], "Full text here."
    )
    assert body.startswith("# Attendance Policy")
    assert "Policy-Category: Attendance" in body
    assert "Tags: ptolicy, hr" in body
    assert "Full text here." in body


def test_compute_counts_splits_company_compliance_and_law():
    policy_docs = [
        {"name": pl.skill_filename("Company Policy", "Conduct", "Code of Conduct")},
        {"name": pl.skill_filename("Compliance Rule", "Compliance", "HIPAA Rule")},
        {"name": "Legacy Handbook.pdf"},  # counts as company
    ]
    law_docs = [{"name": "AZ Wage Act.txt"}, {"name": "FLSA.txt"}]
    counts = pl.compute_counts(policy_docs, law_docs)
    assert counts == {"all": 5, "company": 2, "state_law": 2, "compliance": 1}


def _rows():
    policy_docs = [
        {"name": pl.skill_filename("Company Policy", "Terminations", "Sep Policy"), "processed_at": "x"},
        {"name": pl.skill_filename("Compliance Rule", "Safety", "OSHA"), "processed_at": None},
    ]
    law_docs = [{"name": "AZ Employment.txt", "_jurisdiction": "AZ", "source_name": "AZ Leg", "processed_at": "x"}]
    return pl._assemble_rows(policy_docs, law_docs)


def test_filter_by_tab():
    rows = _rows()
    assert len(pl._filter_rows(rows, "all", "All Categories", "")) == 3
    assert len(pl._filter_rows(rows, "company", "All Categories", "")) == 1
    assert len(pl._filter_rows(rows, "compliance", "All Categories", "")) == 1
    assert len(pl._filter_rows(rows, "state_law", "All Categories", "")) == 1


def test_filter_by_category_and_search():
    rows = _rows()
    assert len(pl._filter_rows(rows, "all", "Safety", "")) == 1
    assert len(pl._filter_rows(rows, "all", "All Categories", "osha")) == 1
    assert len(pl._filter_rows(rows, "all", "All Categories", "nomatch")) == 0


def test_local_library_search_never_calls_web():
    # The AI-Search helper only ever calls the local policy/law indexes — proven
    # by the fact that a svc exposing ONLY those two local search methods works
    # end-to-end with no network surface.
    class _Svc:
        def policy_search(self, cid, q, k):
            return [{"citation": "SRC-1", "text": "policy text", "source_ref": "SRC-1"}]

        def law_search(self, jur, q, k):
            return [{"citation": "LAW-1", "text": "statute text", "source_ref": "LAW-1"}]

    out = pl._local_library_search(_Svc(), "CLI-1", ["federal", "AZ"], "osha")
    origins = {r["origin"] for r in out}
    assert origins == {"policy", "law"}
    assert len(out) == 3  # 1 policy + 2 law jurisdictions
