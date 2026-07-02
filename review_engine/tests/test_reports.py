from review_engine.audits.database import ReviewDatabase
from review_engine.extraction.models import SourceChunk
from review_engine.reports.generator import generate_docx_report, generate_pdf_report


def test_docx_and_pdf_report_generation(tmp_path):
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
    docx = generate_docx_report(db, matter_id)
    pdf = generate_pdf_report(db, matter_id)
    assert docx.startswith(b"PK")
    assert pdf.startswith(b"%PDF")
