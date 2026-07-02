from __future__ import annotations
import re
from pathlib import Path
from typing import Iterable
import pandas as pd
from docx import Document
from review_engine.config.settings import CHUNK_OVERLAP, CHUNK_SIZE
from review_engine.extraction.models import SourceChunk, source_reference


def _split_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    clean=re.sub(r"[ \t]+"," ",text).strip(); chunks=[]; start=0
    while start < len(clean):
        end=min(len(clean),start+size)
        if end < len(clean):
            boundary=max(clean.rfind("\n",start,end),clean.rfind(". ",start,end))
            if boundary > start+size//2: end=boundary+1
        chunks.append(clean[start:end].strip())
        if end >= len(clean): break
        start=max(start+1,end-overlap)
    return chunks

def _make_chunks(matter_id,path,texts):
    result=[]; ordinal=0
    for text,page,row,section in texts:
        for part in _split_text(text):
            result.append(SourceChunk(matter_id,path.name,path.suffix.lower().lstrip("."),part,source_reference(matter_id,path.name,page=page,row=row,section=section,ordinal=ordinal),page,row,section)); ordinal+=1
    return result

def _extract_pdf(path):
    import fitz
    output=[]
    with fitz.open(path) as pdf:
        for number,page in enumerate(pdf,start=1):
            text=page.get_text("text"); tables=[]
            try:
                for table in page.find_tables().tables: tables.append("\n".join(" | ".join(str(v or "") for v in row) for row in table.extract()))
            except (AttributeError,TypeError,ValueError): pass
            output.append((text+("\nTABLE:\n"+"\n".join(tables) if tables else ""),number,None,None))
    return output

def _extract_docx(path):
    doc=Document(path); output=[]; section="Document body"; buffer=[]
    for paragraph in doc.paragraphs:
        text=paragraph.text.strip()
        if not text: continue
        if paragraph.style and paragraph.style.name.startswith("Heading"):
            if buffer: output.append(("\n".join(buffer),None,None,section)); buffer=[]
            section=text
        else: buffer.append(text)
    if buffer: output.append(("\n".join(buffer),None,None,section))
    for index,table in enumerate(doc.tables,start=1): output.append(("\n".join(" | ".join(c.text for c in row.cells) for row in table.rows),None,None,f"Table {index}"))
    return output

def _extract_sheet(path):
    sheets=pd.read_excel(path,sheet_name=None,dtype=str) if path.suffix.lower()==".xlsx" else {"CSV":pd.read_csv(path,dtype=str,keep_default_na=False)}; output=[]
    for name,frame in sheets.items():
        frame=frame.fillna("")
        for pos,(_,record) in enumerate(frame.iterrows(),start=2): output.append((" | ".join(f"{col}: {record[col]}" for col in frame.columns),None,pos,str(name)))
    return output

def extract_document(path: str|Path, matter_id: str):
    path=Path(path); ext=path.suffix.lower()
    if ext==".pdf": texts=_extract_pdf(path)
    elif ext==".docx": texts=_extract_docx(path)
    elif ext in {".csv",".xlsx"}: texts=_extract_sheet(path)
    elif ext==".txt": texts=[(path.read_text(encoding="utf-8",errors="replace"),None,None,"Document body")]
    else: raise ValueError(f"Unsupported file type: {ext}")
    return _make_chunks(matter_id,path,texts)
