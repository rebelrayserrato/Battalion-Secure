from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from review_engine.config.settings import DATABASE_PATH, ensure_directories
from review_engine.extraction.models import SourceChunk


class ReviewDatabase:
    def __init__(self, path: str | Path = DATABASE_PATH):
        ensure_directories()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        # RAYAAAA-210: the Streamlit UI and the matter-API sidecar are two
        # processes sharing this file. A busy timeout makes a concurrent writer
        # wait for the lock instead of failing immediately with "database is
        # locked". Writes here are tiny and short, so 5s is ample headroom.
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS matters (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
                    jurisdiction TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, matter_id TEXT NOT NULL,
                    name TEXT NOT NULL, path TEXT NOT NULL, file_type TEXT NOT NULL,
                    size INTEGER, uploaded_at TEXT NOT NULL, processed_at TEXT,
                    UNIQUE(matter_id, name), FOREIGN KEY(matter_id) REFERENCES matters(id)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    source_ref TEXT PRIMARY KEY, matter_id TEXT NOT NULL,
                    document_name TEXT NOT NULL, file_type TEXT NOT NULL,
                    page INTEGER, row_number INTEGER, section TEXT, text TEXT NOT NULL,
                    FOREIGN KEY(matter_id) REFERENCES matters(id)
                );
                CREATE TABLE IF NOT EXISTS entities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, matter_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL, value TEXT NOT NULL, source_ref TEXT NOT NULL,
                    FOREIGN KEY(source_ref) REFERENCES chunks(source_ref)
                );
                CREATE TABLE IF NOT EXISTS findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, matter_id TEXT NOT NULL,
                    title TEXT NOT NULL, category TEXT NOT NULL, explanation TEXT NOT NULL,
                    sources_json TEXT NOT NULL, confidence TEXT NOT NULL,
                    confidence_reason TEXT NOT NULL, human_review_required INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, matter_id TEXT,
                    event_type TEXT NOT NULL, details TEXT, timestamp TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def log(self, event_type: str, matter_id: str | None = None, details: str = "") -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO audit_logs(matter_id,event_type,details,timestamp) VALUES(?,?,?,?)",
                (matter_id, event_type, details, self.now()),
            )

    def create_matter(
        self, name: str, description: str = "", jurisdiction: str = ""
    ) -> str:
        matter_id = f"MAT-{uuid.uuid4().hex[:10].upper()}"
        with self.connect() as db:
            db.execute(
                "INSERT INTO matters VALUES(?,?,?,?,?)",
                (matter_id, name.strip(), description.strip(), jurisdiction.strip(), self.now()),
            )
        self.log("matter_created", matter_id, name)
        return matter_id

    def list_matters(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM matters ORDER BY created_at DESC")]

    def get_matter(self, matter_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM matters WHERE id=?", (matter_id,)).fetchone()
            return dict(row) if row else None

    def add_document(self, matter_id: str, name: str, path: Path) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO documents(matter_id,name,path,file_type,size,uploaded_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(matter_id,name) DO UPDATE SET
                   path=excluded.path,size=excluded.size,uploaded_at=excluded.uploaded_at,
                   processed_at=NULL""",
                (matter_id, name, str(path), path.suffix.lower(), path.stat().st_size, self.now()),
            )
        self.log("file_uploaded", matter_id, name)

    def list_documents(self, matter_id: str) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM documents WHERE matter_id=? ORDER BY name", (matter_id,)
                )
            ]

    def replace_document_chunks(self, matter_id: str, document_name: str, chunks: Iterable[SourceChunk]) -> None:
        chunk_list = list(chunks)
        with self.connect() as db:
            old_refs = [
                row[0]
                for row in db.execute(
                    "SELECT source_ref FROM chunks WHERE matter_id=? AND document_name=?",
                    (matter_id, document_name),
                )
            ]
            if old_refs:
                db.executemany("DELETE FROM entities WHERE source_ref=?", [(ref,) for ref in old_refs])
            db.execute(
                "DELETE FROM chunks WHERE matter_id=? AND document_name=?",
                (matter_id, document_name),
            )
            db.executemany(
                """INSERT INTO chunks(source_ref,matter_id,document_name,file_type,page,row_number,section,text)
                   VALUES(?,?,?,?,?,?,?,?)""",
                [
                    (
                        c.source_ref, c.matter_id, c.document_name, c.file_type,
                        c.page, c.row, c.section, c.text,
                    )
                    for c in chunk_list
                ],
            )
            db.execute(
                "UPDATE documents SET processed_at=? WHERE matter_id=? AND name=?",
                (self.now(), matter_id, document_name),
            )
        self.log("file_processed", matter_id, f"{document_name}: {len(chunk_list)} chunks")

    def get_chunks(self, matter_id: str) -> list[SourceChunk]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM chunks WHERE matter_id=? ORDER BY document_name,page,row_number",
                (matter_id,),
            )
            return [
                SourceChunk(
                    matter_id=row["matter_id"], document_name=row["document_name"],
                    file_type=row["file_type"], page=row["page"], row=row["row_number"],
                    section=row["section"], text=row["text"], source_ref=row["source_ref"],
                )
                for row in rows
            ]

    def replace_entities(self, matter_id: str, entities: list[dict]) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM entities WHERE matter_id=?", (matter_id,))
            db.executemany(
                "INSERT INTO entities(matter_id,entity_type,value,source_ref) VALUES(?,?,?,?)",
                [(matter_id, e["entity_type"], e["value"], e["source_ref"]) for e in entities],
            )

    def get_entities(self, matter_id: str) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM entities WHERE matter_id=? ORDER BY entity_type,value",
                    (matter_id,),
                )
            ]

    def replace_findings(self, matter_id: str, findings: list[dict]) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM findings WHERE matter_id=?", (matter_id,))
            db.executemany(
                """INSERT INTO findings(matter_id,title,category,explanation,sources_json,
                   confidence,confidence_reason,human_review_required,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        matter_id, f["title"], f["category"], f["explanation"],
                        json.dumps(f["supporting_sources"]), f["confidence"],
                        f["confidence_reason"], int(f["human_review_required"]), self.now(),
                    )
                    for f in findings
                ],
            )

    def get_findings(self, matter_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM findings WHERE matter_id=? ORDER BY category,title", (matter_id,))
            result = []
            for row in rows:
                item = dict(row)
                item["supporting_sources"] = json.loads(item.pop("sources_json"))
                item["human_review_required"] = bool(item["human_review_required"])
                result.append(item)
            return result

    def get_audit_log(self, matter_id: str) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM audit_logs WHERE matter_id=? ORDER BY timestamp DESC",
                    (matter_id,),
                )
            ]
