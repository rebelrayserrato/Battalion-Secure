from review_engine.extraction.extractors import extract_document
from review_engine.fraud_detection.review import review_spreadsheet


def test_spreadsheet_duplicate_invoice_and_amount_flags(tmp_path):
    path = tmp_path / "ledger.csv"
    path.write_text(
        "invoice,vendor,amount,approver\nINV-1,Acme,1000,Lee\n"
        "INV-1,Acme,1000,Lee\nINV-2,Acme,250,Lee\n",
        encoding="utf-8",
    )
    chunks = extract_document(path, "MAT-TEST")
    candidates = review_spreadsheet(path, chunks)
    titles = {candidate["title"] for candidate in candidates}
    assert "Potential duplicate invoices" in titles
    assert "Potential duplicate payment amounts" in titles
    assert all(candidate["sources"] for candidate in candidates)
    assert all("Potential fraud indicator" in candidate["explanation"] for candidate in candidates)
