from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import pandas as pd
from docx import Document

from review_engine.config.settings import CHUNK_OVERLAP, CHUNK_SIZE
from review_engine.extraction.models import SourceChunk, source_reference


def _split_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    clean = re.sub(r"[ \t]+", " ", text).strip()
    if not clean:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(clean):
        end = min(len(clean), start + size)
        if end < len(clean):
            boundary = max(clean.rfind("\n", start, end), clean.rfind(". ", start, end))
            if boundary > start + size // 2:
                end = boundary + 1
        chunks.append(clean[start:end].strip())
        if end >= len(clean):
            break
        start = max(start + 1, end - overlap)
    return chunks


def _make_chunks(
    matter_id: str,
    path: Path,
    texts: Iterable[tuple[str, int | None, int | None, str | None]],
) -> list[SourceChunk]:
    result: list[SourceChunk] = []
    ordinal = 0
    for text, page, row, section in texts:
        for part in _split_text(text):
            result.append(
                SourceChunk(
                    matter_id=matter_id,
                    document_name=path.name,
                    file_type=path.suffix.lower().lstrip("."),
                    page=page,
                    row=row,
                    section=section,
                    text=part,
                    source_ref=source_reference(
                        matter_id,
                        path.name,
                        page=page,
                        row=row,
                        section=section,
                        ordinal=ordinal,
                    ),
                )
            )
            ordinal += 1
    return result


def _extract_pdf(path: Path) -> list[tuple[str, int | None, int | None, str | None]]:
    import fitz

    output = []
    with fitz.open(path) as pdf:
        for number, page in enumerate(pdf, start=1):
            text = page.get_text("text")
            tables = []
            try:
                for table in page.find_tables().tables:
                    tables.append("\n".join(" | ".join(str(v or "") for v in row) for row in table.extract()))
            except (AttributeError, TypeError, ValueError):
                pass
            combined = text + ("\nTABLE:\n" + "\n".join(tables) if tables else "")
            output.append((combined, number, None, None))
    return output


def _extract_docx(path: Path) -> list[tuple[str, int | None, int | None, str | None]]:
    doc = Document(path)
    output = []
    current_section = "Document body"
    buffer: list[str] = []
    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        if paragraph.style and paragraph.style.name.startswith("Heading"):
            if buffer:
                output.append(("\n".join(buffer), None, None, current_section))
                buffer = []
            current_section = text
        else:
            buffer.append(text)
    if buffer:
        output.append(("\n".join(buffer), None, None, current_section))
    for index, table in enumerate(doc.tables, start=1):
        text = "\n".join(" | ".join(cell.text for cell in row.cells) for row in table.rows)
        output.append((text, None, None, f"Table {index}"))
    return output


def _extract_spreadsheet(path: Path) -> list[tuple[str, int | None, int | None, str | None]]:
    sheets = (
        pd.read_excel(path, sheet_name=None, dtype=str)
        if path.suffix.lower() == ".xlsx"
        else {"CSV": pd.read_csv(path, dtype=str, keep_default_na=False)}
    )
    output = []
    for sheet_name, frame in sheets.items():
        frame = frame.fillna("")
        for position, (_, record) in enumerate(frame.iterrows(), start=2):
            text = " | ".join(f"{column}: {record[column]}" for column in frame.columns)
            output.append((text, None, position, str(sheet_name)))
    return output


def extract_document(path: str | Path, matter_id: str) -> list[SourceChunk]:
    path = Path(path)
    extension = path.suffix.lower()
    if extension == ".pdf":
        texts = _extract_pdf(path)
    elif extension == ".docx":
        texts = _extract_docx(path)
    elif extension in {".csv", ".xlsx"}:
        texts = _extract_spreadsheet(path)
    elif extension == ".txt":
        texts = [(path.read_text(encoding="utf-8", errors="replace"), None, None, "Document body")]
    else:
        raise ValueError(f"Unsupported file type: {extension}")
    return _make_chunks(matter_id, path, texts)
