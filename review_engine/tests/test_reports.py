import io
import json
from datetime import datetime, timezone
from pathlib import Path

from docx import Document

from review_engine.audits.database import ReviewDatabase
from review_engine.extraction.models import SourceChunk
from review_engine.reports.decisions import (
    default_decisions_path,
    finding_decision,
    load_decisions,
    normalize_status,
    rollup,
)
from review_engine.reports.generator import (
    GOLD,
    NAVY,
    WORDMARK,
    generate_docx_report,
    generate_pdf_report,
)

FIXED_TS = datetime(2026, 7, 12, 9, 30, tzinfo=timezone.utc)


def _seed_matter(tmp_path):
    db = ReviewDatabase(tmp_path / "test.sqlite3")
    matter_id = db.create_matter("Report Test")
    source = tmp_path / "evidence.txt"
    source.write_text("Termination occurred on 01/03/2025.", encoding="utf-8")
    db.add_document(matter_id, source.name, source)
    chunk = SourceChunk(
        matter_id, source.name, "txt", source.read_text(), "SRC-REPORT",
        section="Document body",
    )
    db.replace_document_chunks(matter_id, source.name, [chunk])
    db.replace_findings(
        matter_id,
        [
            {
                "title": "Termination risk",
                "category": "HR Legal Risk",
                "explanation": "Requires human review.",
                "supporting_sources": [
                    {
                        "source_ref": chunk.source_ref,
                        "document_name": chunk.document_name,
                        "page": None,
                        "row": None,
                        "section": chunk.section,
                        "citation": chunk.citation,
                    }
                ],
                "confidence": "Medium",
                "confidence_reason": "Direct term match.",
                "human_review_required": True,
            }
        ],
    )
    return db, matter_id


def _docx_text(content: bytes) -> str:
    document = Document(io.BytesIO(content))
    return "\n".join(p.text for p in document.paragraphs)


def test_docx_and_pdf_report_generation(tmp_path):
    db, matter_id = _seed_matter(tmp_path)
    docx = generate_docx_report(db, matter_id)
    pdf = generate_pdf_report(db, matter_id)
    assert docx.startswith(b"PK")
    assert pdf.startswith(b"%PDF")


def test_report_without_decisions_omits_reviewer_section(tmp_path):
    db, matter_id = _seed_matter(tmp_path)
    docx = generate_docx_report(db, matter_id, generated_at=FIXED_TS)
    text = _docx_text(docx)
    # Branding present (AC1)
    assert WORDMARK in text
    assert "Evidence Review Report" in text
    assert "Task: Report Test" in text
    assert "Generated: 2026-07-12 09:30 UTC" in text
    # Findings carry SRC IDs (AC2)
    assert "SRC-REPORT" in text
    # No reviewer section, no rollup, and no crash (AC3)
    assert "Reviewer Decisions" not in text
    assert "Reviewer decisions:" not in text
    # PDF path also renders without decisions
    assert generate_pdf_report(db, matter_id, generated_at=FIXED_TS).startswith(b"%PDF")


def test_report_with_decisions_renders_reviewer_section_and_rollup(tmp_path):
    db, matter_id = _seed_matter(tmp_path)
    decisions = {
        "task_id": matter_id,
        "decisions": {
            "SRC-REPORT": {
                "status": "approve",
                "note": "Confirmed against source.",
                "decided_at": "2026-07-12T10:00:00Z",
                "reviewer": "J. Reviewer",
            }
        },
    }
    docx = generate_docx_report(db, matter_id, decisions=decisions, generated_at=FIXED_TS)
    text = _docx_text(docx)
    assert "Reviewer Decisions" in text
    assert "Termination risk — Approved [SRC-REPORT]" in text
    assert "Confirmed against source." in text
    assert "reviewer J. Reviewer" in text
    # Rollup in the summary AND in the reviewer section (AC5)
    assert "Reviewer decisions: 1 approved, 0 rejected, 0 needs-changes, 0 undecided." in text
    # PDF also renders with decisions
    assert generate_pdf_report(db, matter_id, decisions=decisions, generated_at=FIXED_TS).startswith(b"%PDF")


def test_report_reads_decisions_file_from_disk(tmp_path):
    db, matter_id = _seed_matter(tmp_path)
    path = default_decisions_path(db.path, matter_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "task_id": matter_id,
                "decisions": {
                    "SRC-REPORT": {
                        "status": "reject",
                        "note": "Source does not support the flag.",
                        "decided_at": "2026-07-12T11:00:00Z",
                        "reviewer": "K. Auditor",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    docx = generate_docx_report(db, matter_id, decisions=path, generated_at=FIXED_TS)
    text = _docx_text(docx)
    assert "Termination risk — Rejected [SRC-REPORT]" in text
    assert "Reviewer decisions: 0 approved, 1 rejected, 0 needs-changes, 0 undecided." in text


def test_missing_decisions_file_degrades_gracefully(tmp_path):
    db, matter_id = _seed_matter(tmp_path)
    missing = tmp_path / "does_not_exist.json"
    docx = generate_docx_report(db, matter_id, decisions=missing, generated_at=FIXED_TS)
    text = _docx_text(docx)
    assert "Reviewer Decisions" not in text
    assert WORDMARK in text  # still branded


def test_output_is_deterministic_for_fixed_input(tmp_path):
    db, matter_id = _seed_matter(tmp_path)
    decisions = {"task_id": matter_id, "decisions": {"SRC-REPORT": {"status": "approve"}}}
    docx_a = generate_docx_report(db, matter_id, decisions=decisions, generated_at=FIXED_TS)
    docx_b = generate_docx_report(db, matter_id, decisions=decisions, generated_at=FIXED_TS)
    assert docx_a == docx_b
    pdf_a = generate_pdf_report(db, matter_id, decisions=decisions, generated_at=FIXED_TS)
    pdf_b = generate_pdf_report(db, matter_id, decisions=decisions, generated_at=FIXED_TS)
    assert pdf_a == pdf_b


def test_palette_constants_match_rayserr_theme():
    # Navy/gold from RAYAAAA-227 admin.css theme.
    assert NAVY == "#1b2f5b"
    assert GOLD == "#c8922a"


def test_decisions_helpers():
    assert normalize_status("approve") == "approved"
    assert normalize_status("needs_changes") == "needs-changes"
    assert normalize_status(None) == "undecided"
    assert normalize_status("garbage") == "undecided"
    # Full envelope and bare mapping both accepted
    assert load_decisions({"decisions": {"S1": {"status": "reject"}}})["S1"]["status"] == "rejected"
    assert load_decisions({"S1": {"status": "approve"}})["S1"]["status"] == "approved"
    # task_id mismatch -> ignored
    assert load_decisions({"task_id": "A", "decisions": {"S1": {"status": "approve"}}}, "B") == {}
    assert load_decisions(None) == {}
    finding = {"supporting_sources": [{"source_ref": "S1"}]}
    decisions = {"S1": {"status": "approved", "note": "", "decided_at": "", "reviewer": ""}}
    assert finding_decision(finding, decisions)[0] == "S1"
    assert rollup([finding], decisions)["approved"] == 1
    assert rollup([finding], {})["undecided"] == 1


def test_fixture_file_is_loadable():
    fixture = Path(__file__).parent / "fixtures" / "decisions_sample.json"
    loaded = load_decisions(fixture)
    assert loaded["SRC-REPORT"]["status"] == "approved"
    assert loaded["SRC-OTHER"]["status"] == "needs-changes"
