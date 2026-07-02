from __future__ import annotations

from io import BytesIO
from pathlib import Path

from docx import Document
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer
from xml.sax.saxutils import escape

SECTIONS = [
    "Executive Summary",
    "Documents Reviewed",
    "Timeline",
    "Key People and Entities",
    "Missing Documents",
    "HR / Legal Risk Flags",
    "Fraud Red Flags",
    "Contradictions",
    "Unsupported or Low-Confidence Findings",
    "Human Review Required",
    "Recommended Next Steps",
    "Source Appendix",
    "Audit Log Summary",
]


def _report_data(db, matter_id: str, executive_summary: str | None = None) -> dict:
    matter = db.get_matter(matter_id)
    if not matter:
        raise ValueError(f"Unknown matter: {matter_id}")
    documents = db.list_documents(matter_id)
    findings = db.get_findings(matter_id)
    chunks = db.get_chunks(matter_id)
    from review_engine.evidence.timeline import build_timeline

    timeline = build_timeline(chunks)
    entities = db.get_entities(matter_id)
    audits = db.get_audit_log(matter_id)
    summary = executive_summary or (
        f"This evidence review identified {len(findings)} source-supported review flags "
        f"across {len(documents)} document(s). Findings are screening indicators only, "
        "not legal conclusions or determinations that fraud occurred."
    )
    grouped = {
        "Missing Documents": [f for f in findings if f["category"] == "Missing Document"],
        "HR / Legal Risk Flags": [f for f in findings if f["category"] == "HR Legal Risk"],
        "Fraud Red Flags": [f for f in findings if f["category"] == "Fraud Red Flag"],
        "Contradictions": [f for f in findings if f["category"] == "Contradiction"],
        "Unsupported or Low-Confidence Findings": [
            f for f in findings if f["category"] == "Unsupported Finding" or f["confidence"] == "Low"
        ],
        "Human Review Required": [f for f in findings if f["human_review_required"]],
    }
    return {
        "matter": matter, "documents": documents, "findings": findings, "chunks": chunks,
        "timeline": timeline, "entities": entities, "audits": audits, "summary": summary,
        "grouped": grouped,
    }


def _finding_text(finding: dict) -> str:
    citations = "; ".join(source["citation"] for source in finding["supporting_sources"])
    return (
        f"{finding['title']} [{finding['confidence']} confidence] — {finding['explanation']} "
        f"Confidence basis: {finding['confidence_reason']} Sources: {citations}"
    )


def _section_items(data: dict, section: str) -> list[str]:
    if section == "Executive Summary":
        return [data["summary"]]
    if section == "Documents Reviewed":
        return [
            f"{doc['name']} ({doc['file_type']}); processed: {doc['processed_at'] or 'not processed'}"
            for doc in data["documents"]
        ] or ["No documents."]
    if section == "Timeline":
        return [
            f"{item['date']} — {item['event']} Source: {item['citation']}"
            for item in data["timeline"]
        ] or ["No dated events identified."]
    if section == "Key People and Entities":
        return [
            f"{entity['entity_type']}: {entity['value']} ({entity['source_ref']})"
            for entity in data["entities"]
            if entity["entity_type"] != "event"
        ] or ["No entities identified."]
    if section in data["grouped"]:
        return [_finding_text(f) for f in data["grouped"][section]] or ["No source-supported findings."]
    if section == "Recommended Next Steps":
        return [
            "Have a qualified human reviewer validate each cited source and resolve contradictions.",
            "Confirm document-set completeness and the applicable jurisdiction.",
            "Do not treat anomaly scores or red flags as proof of wrongdoing.",
        ]
    if section == "Source Appendix":
        return [f"{chunk.source_ref}: {chunk.citation} — {chunk.text[:300]}" for chunk in data["chunks"]]
    if section == "Audit Log Summary":
        return [
            f"{entry['timestamp']} — {entry['event_type']}: {entry['details'] or ''}"
            for entry in data["audits"]
        ] or ["No audit entries."]
    return []


def generate_docx_report(db, matter_id: str, output: str | Path | None = None, executive_summary: str | None = None) -> bytes:
    data = _report_data(db, matter_id, executive_summary)
    document = Document()
    document.add_heading("Evidence Review Report", 0)
    document.add_paragraph(f"Matter: {data['matter']['name']} ({matter_id})")
    document.add_paragraph(
        f"Jurisdiction: {data['matter']['jurisdiction'] or 'Required / not specified'}"
    )
    document.add_paragraph(
        "Human-review document. This report does not provide legal advice and does not conclude that fraud occurred."
    )
    for section in SECTIONS:
        document.add_heading(section, level=1)
        for item in _section_items(data, section):
            document.add_paragraph(item, style="List Bullet")
    buffer = BytesIO()
    document.save(buffer)
    content = buffer.getvalue()
    if output:
        Path(output).write_bytes(content)
    return content


def generate_pdf_report(db, matter_id: str, output: str | Path | None = None, executive_summary: str | None = None) -> bytes:
    data = _report_data(db, matter_id, executive_summary)
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=LETTER, rightMargin=0.65 * inch, leftMargin=0.65 * inch,
        topMargin=0.65 * inch, bottomMargin=0.65 * inch,
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph("Evidence Review Report", styles["Title"]),
        Paragraph(escape(f"Matter: {data['matter']['name']} ({matter_id})"), styles["Normal"]),
        Paragraph(
            escape(f"Jurisdiction: {data['matter']['jurisdiction'] or 'Required / not specified'}"),
            styles["Normal"],
        ),
        Paragraph(
            "Human-review document. This report does not provide legal advice and does not conclude that fraud occurred.",
            styles["Italic"],
        ),
        Spacer(1, 12),
    ]
    for section in SECTIONS:
        story.append(Paragraph(escape(section), styles["Heading1"]))
        for item in _section_items(data, section):
            story.append(Paragraph("• " + escape(item), styles["BodyText"]))
            story.append(Spacer(1, 5))
    doc.build(story)
    content = buffer.getvalue()
    if output:
        Path(output).write_bytes(content)
    return content
