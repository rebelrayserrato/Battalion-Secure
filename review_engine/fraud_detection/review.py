from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from review_engine.extraction.models import SourceChunk


def _column(frame: pd.DataFrame, terms: tuple[str, ...]) -> str | None:
    for name in frame.columns:
        normalized = str(name).lower().replace("_", " ")
        if any(term in normalized for term in terms):
            return str(name)
    return None


def _source_for_row(chunks: list[SourceChunk], document_name: str, row: int) -> list[SourceChunk]:
    return [c for c in chunks if c.document_name == document_name and c.row == row]


def review_spreadsheet(path: str | Path, chunks: list[SourceChunk]) -> list[dict]:
    path = Path(path)
    sheets = (
        pd.read_excel(path, sheet_name=None)
        if path.suffix.lower() == ".xlsx"
        else {"CSV": pd.read_csv(path)}
    )
    candidates: list[dict] = []
    for sheet, raw in sheets.items():
        if raw.empty:
            continue
        frame = raw.copy()
        invoice_col = _column(frame, ("invoice", "reference number"))
        amount_col = _column(frame, ("amount", "total", "payment", "expense", "gross", "net"))
        vendor_col = _column(frame, ("vendor", "supplier", "payee", "employee"))
        approval_col = _column(frame, ("approver", "approved by", "approval"))

        if invoice_col:
            duplicates = frame[invoice_col].notna() & frame[invoice_col].duplicated(keep=False)
            for value, group in frame[duplicates].groupby(invoice_col):
                rows = [int(i) + 2 for i in group.index]
                sources = sum((_source_for_row(chunks, path.name, row) for row in rows), [])
                candidates.append(
                    _candidate(
                        "Potential duplicate invoices",
                        f"Invoice identifier {value!s} appears on spreadsheet rows {rows}.",
                        sources,
                        "High",
                        "The same non-empty invoice identifier appears more than once.",
                    )
                )

        numeric = None
        if amount_col:
            numeric = pd.to_numeric(
                frame[amount_col].astype(str).str.replace(r"[$,]", "", regex=True), errors="coerce"
            )
            duplicate_amounts = numeric.notna() & numeric.duplicated(keep=False)
            for value, group in frame[duplicate_amounts].groupby(numeric[duplicate_amounts]):
                rows = [int(i) + 2 for i in group.index]
                sources = sum((_source_for_row(chunks, path.name, row) for row in rows), [])
                candidates.append(
                    _candidate(
                        "Potential duplicate payment amounts",
                        f"Amount ${value:,.2f} repeats on spreadsheet rows {rows}. Repetition alone does not establish impropriety.",
                        sources,
                        "Medium",
                        "The numeric amount repeats; legitimate recurring payments remain possible.",
                    )
                )

            round_rows = frame.index[numeric.notna() & (numeric.abs() >= 100) & (numeric.mod(100) == 0)]
            if len(round_rows) >= 2:
                rows = [int(i) + 2 for i in round_rows]
                sources = sum((_source_for_row(chunks, path.name, row) for row in rows[:10]), [])
                candidates.append(
                    _candidate(
                        "Repeated round-dollar amounts",
                        f"{len(rows)} payments are round-dollar amounts of at least $100.",
                        sources,
                        "Medium",
                        "Multiple round-dollar transactions were observed; context is required.",
                    )
                )

            valid = numeric.dropna()
            if len(valid) >= 5 and valid.nunique() > 1:
                import numpy as np
                from sklearn.ensemble import IsolationForest

                model = IsolationForest(random_state=42, contamination="auto")
                predictions = model.fit_predict(valid.to_numpy().reshape(-1, 1))
                scores = -model.score_samples(valid.to_numpy().reshape(-1, 1))
                anomalous_positions = np.where(predictions == -1)[0]
                for position in anomalous_positions[:10]:
                    index = valid.index[position]
                    row = int(index) + 2
                    sources = _source_for_row(chunks, path.name, row)
                    candidates.append(
                        _candidate(
                            "Unusual payment amount",
                            f"Amount ${valid.loc[index]:,.2f} on row {row} received an Isolation Forest anomaly score of {scores[position]:.3f}.",
                            sources,
                            "Medium",
                            "A data-based outlier score was generated from this spreadsheet only.",
                        )
                    )

        if approval_col:
            blank = frame[approval_col].isna() | frame[approval_col].astype(str).str.strip().eq("")
            for index in frame.index[blank][:10]:
                row = int(index) + 2
                candidates.append(
                    _candidate(
                        "Missing or incomplete approval",
                        f"Approval data is blank on spreadsheet row {row}.",
                        _source_for_row(chunks, path.name, row),
                        "High",
                        "The approval field is blank in the cited row.",
                    )
                )
            if vendor_col:
                pairs = frame[[vendor_col, approval_col]].dropna().astype(str)
                counts = pairs.groupby([vendor_col, approval_col]).size()
                for (vendor, approver), count in counts[counts >= 3].items():
                    rows = [
                        int(i) + 2
                        for i in pairs.index[
                            (pairs[vendor_col] == vendor) & (pairs[approval_col] == approver)
                        ]
                    ]
                    sources = sum((_source_for_row(chunks, path.name, row) for row in rows[:5]), [])
                    candidates.append(
                        _candidate(
                            "Repeated vendor-approver pattern",
                            f"{approver} approved {count} entries associated with {vendor}. This pattern requires contextual review.",
                            sources,
                            "Low",
                            "The repeated relationship is observed, but role assignments may explain it.",
                        )
                    )
    return candidates


def _candidate(title, explanation, sources, confidence, reason) -> dict:
    return {
        "title": title,
        "category": "Fraud Red Flag",
        "explanation": "Potential fraud indicator: " + explanation,
        "sources": sources,
        "confidence": confidence,
        "confidence_reason": reason,
        "human_review_required": True,
    }


def run_fraud_review(document_paths: list[str | Path], chunks: list[SourceChunk]) -> list[dict]:
    candidates = []
    for path in document_paths:
        if Path(path).suffix.lower() in {".csv", ".xlsx"}:
            candidates.extend(review_spreadsheet(path, chunks))

    patterns = [
        ("Missing support documents", ("missing receipt", "no receipt", "supporting document unavailable")),
        ("Payroll/timecard anomaly", ("timecard discrepancy", "overtime discrepancy", "payroll adjustment")),
        ("Expense anomaly", ("expense discrepancy", "personal expense", "unreconciled expense")),
        ("Metadata mismatch placeholder", ("metadata mismatch", "created after approval", "modified date mismatch")),
        ("Vendor/entity relationship red flag", ("related party", "same address as vendor", "employee-owned vendor")),
    ]
    for title, terms in patterns:
        sources = [c for c in chunks if any(term in c.text.lower() for term in terms)]
        if sources:
            candidates.append(
                _candidate(
                    title,
                    "Relevant indicator language appears in the processed evidence.",
                    sources[:5],
                    "Medium",
                    "Direct keyword evidence is present, but context and underlying records require review.",
                )
            )
    timeline_terms = (
        "payment before approval",
        "approved before submitted",
        "approval predates",
        "terminated before investigation",
    )
    timeline_sources = [
        chunk for chunk in chunks if any(term in chunk.text.lower() for term in timeline_terms)
    ]
    if timeline_sources:
        candidates.append(
            {
                "title": "Timeline inconsistency",
                "category": "Timeline Issue",
                "explanation": "A potentially inconsistent event sequence appears in the evidence and requires human review.",
                "sources": timeline_sources[:5],
                "confidence": "Medium",
                "confidence_reason": "Explicit sequence-conflict language is present; actual event dates need validation.",
                "human_review_required": True,
            }
        )
    return candidates
