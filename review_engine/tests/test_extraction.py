import zipfile

import pytest

from review_engine.extraction import extractors, ocr
from review_engine.extraction.extractors import (
    _safe_zip_member_name,
    extract_document,
)

requires_ocr = pytest.mark.skipif(
    not ocr.ocr_available(), reason="local tesseract OCR toolchain unavailable"
)


def _text_image_bytes(text: str):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (720, 220), "white")
    draw = ImageDraw.Draw(image)
    # Large default font renders legibly enough for tesseract on synthetic text.
    draw.text((20, 80), text, fill="black")
    return image


def test_txt_document_extraction_creates_source_chunks(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("Employee was terminated on January 15, 2025.", encoding="utf-8")
    chunks = extract_document(path, "MAT-TEST")
    assert len(chunks) == 1
    assert chunks[0].document_name == "notes.txt"
    assert chunks[0].section == "Document body"
    assert "terminated" in chunks[0].text
    assert chunks[0].source_ref.startswith("SRC-")


def test_csv_extraction_preserves_spreadsheet_row(tmp_path):
    path = tmp_path / "payments.csv"
    path.write_text("invoice,amount\nA-1,100\nA-2,200\n", encoding="utf-8")
    chunks = extract_document(path, "MAT-TEST")
    assert [chunk.row for chunk in chunks] == [2, 3]
    assert chunks[0].section == "CSV"


@requires_ocr
def test_png_image_ocr_extraction(tmp_path):
    path = tmp_path / "scan.png"
    _text_image_bytes("INVOICE TOTAL 4200").save(path)
    chunks = extract_document(path, "MAT-TEST")
    assert chunks, "expected OCR to yield at least one chunk"
    assert chunks[0].file_type == "png"
    assert chunks[0].section == "OCR text"
    assert chunks[0].source_ref.startswith("SRC-")
    combined = " ".join(chunk.text for chunk in chunks).upper()
    assert "INVOICE" in combined


@requires_ocr
def test_scanned_pdf_falls_back_to_ocr(tmp_path):
    import fitz

    image_path = tmp_path / "page.png"
    _text_image_bytes("TERMINATION NOTICE 2025").save(image_path)
    pdf_path = tmp_path / "scanned.pdf"
    doc = fitz.open()
    page = doc.new_page(width=760, height=260)
    page.insert_image(page.rect, filename=str(image_path))
    doc.save(pdf_path)
    doc.close()

    # Native text layer is empty, so the OCR fallback must engage.
    chunks = extract_document(pdf_path, "MAT-TEST")
    assert chunks, "expected OCR fallback to produce chunks"
    assert chunks[0].page == 1
    assert chunks[0].section == "OCR text"
    combined = " ".join(chunk.text for chunk in chunks).upper()
    assert "TERMINATION" in combined


def test_native_pdf_is_not_ocred(tmp_path):
    import fitz

    pdf_path = tmp_path / "native.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "The contract was signed on March 3, 2025 by both parties.")
    doc.save(pdf_path)
    doc.close()

    chunks = extract_document(pdf_path, "MAT-TEST")
    assert chunks
    assert chunks[0].page == 1
    # A real text layer must never be routed through OCR.
    assert all(chunk.section != "OCR text" for chunk in chunks)
    assert "contract" in " ".join(chunk.text for chunk in chunks)


def test_zip_ingestion_routes_each_member(tmp_path):
    zip_path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("notes.txt", "Employee was terminated on January 15, 2025.")
        archive.writestr("records/payments.csv", "invoice,amount\nA-1,100\nA-2,200\n")
        archive.writestr("ignore.bin", "not a supported type")

    chunks = extract_document(zip_path, "MAT-ZIP")
    names = {chunk.document_name for chunk in chunks}
    assert any(name.startswith("bundle__") and name.endswith("notes.txt") for name in names)
    assert any("payments.csv" in name for name in names)
    # Unsupported member is skipped; every chunk still carries a unique ref.
    refs = [chunk.source_ref for chunk in chunks]
    assert len(refs) == len(set(refs))
    assert all(chunk.matter_id == "MAT-ZIP" for chunk in chunks)


def test_zip_skips_path_traversal_members(tmp_path):
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("good.txt", "safe content about a payment")
        # Force a traversal path into the central directory.
        archive.writestr("../escape.txt", "should never be extracted")

    chunks = extract_document(zip_path, "MAT-ZIP")
    names = {chunk.document_name for chunk in chunks}
    assert any("good.txt" in name for name in names)
    assert not any("escape" in name for name in names)
    # And the traversal member is never written outside tmp_path.
    assert not (tmp_path.parent / "escape.txt").exists()


def test_safe_zip_member_name_rejects_unsafe_paths():
    assert _safe_zip_member_name("../secrets.txt") is None
    assert _safe_zip_member_name("/etc/passwd") is None
    assert _safe_zip_member_name("a/../../b.txt") is None
    assert _safe_zip_member_name("sub/dir/report.pdf") == "sub_dir_report.pdf"


def test_zip_bomb_ratio_guard_rejects_archive(tmp_path, monkeypatch):
    zip_path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("huge.txt", "0" * (2 * 1024 * 1024))  # compresses ~1000x

    monkeypatch.setattr(extractors, "ZIP_MAX_RATIO", 50)
    with pytest.raises(ValueError):
        extract_document(zip_path, "MAT-ZIP")
