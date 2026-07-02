from review_engine.extraction.extractors import extract_document


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
