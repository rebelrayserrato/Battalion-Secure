from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from review_engine.clients.jurisdictions import (
    UNSPECIFIED_STATE,
    normalize_state,
    validate_state,
)
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
                CREATE TABLE IF NOT EXISTS clients (
                    id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    state TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS matters (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
                    jurisdiction TEXT, created_at TEXT NOT NULL,
                    client_id TEXT REFERENCES clients(id)
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
                CREATE TABLE IF NOT EXISTS client_policy_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL,
                    name TEXT NOT NULL, path TEXT NOT NULL, file_type TEXT NOT NULL,
                    size INTEGER, uploaded_at TEXT NOT NULL, processed_at TEXT,
                    UNIQUE(client_id, name), FOREIGN KEY(client_id) REFERENCES clients(id)
                );
                CREATE TABLE IF NOT EXISTS policy_chunks (
                    source_ref TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                    document_name TEXT NOT NULL, file_type TEXT NOT NULL,
                    page INTEGER, row_number INTEGER, section TEXT, text TEXT NOT NULL,
                    FOREIGN KEY(client_id) REFERENCES clients(id)
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
        # RAYAAAA-244: link every matter to a first-class Client and backfill any
        # pre-existing (pre-Client) matters. Idempotent — safe on every boot.
        self._ensure_client_link()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # --- Client concept (RAYAAAA-244, Phase A) -----------------------------
    #
    # A Client owns one or more Tasks/matters and carries the jurisdiction (US
    # state). ``matters.jurisdiction`` is DERIVED from the client at read time
    # (see get_matter/list_matters) so it can never diverge from the client.
    #
    # Identity mapping (documented in docs/RAYAAAA-244-client-concept.md): the
    # Client ``id`` is the SAME client identifier the GDPR erasure fan-out
    # (RAYAAAA-207/223) already uses to group a client's matters — there is no
    # second identity store. ``create_matter(..., client_id=<portal id>)``
    # materializes the Battalion client row keyed by that same identity.

    def _insert_client(
        self, db, display_name: str, state: str, client_id: str | None = None
    ) -> str:
        cid = client_id or f"CLI-{uuid.uuid4().hex[:10].upper()}"
        now = self.now()
        name = (display_name or "").strip() or cid
        db.execute(
            "INSERT INTO clients(id,display_name,state,created_at,updated_at) VALUES(?,?,?,?,?)",
            (cid, name, state, now, now),
        )
        return cid

    def _resolve_client(
        self, db, client_id: str | None, name: str, jurisdiction: str
    ) -> tuple[str, str]:
        """Return (client_id, state) for a matter, creating a client if needed.

        - Explicit known ``client_id`` -> reuse it (state comes from the client).
        - Explicit UNKNOWN ``client_id`` -> materialize a client keyed by that same
          identity (this is how the erasure-fanout client id becomes the Battalion
          client id; no parallel identity store).
        - No ``client_id`` (legacy/producer path) -> create a 1:1 client for this
          matter so the "exactly one client per Task" invariant always holds.
        """
        if client_id:
            row = db.execute(
                "SELECT id, state FROM clients WHERE id=?", (client_id,)
            ).fetchone()
            if row:
                return row["id"], row["state"]
            state = normalize_state(jurisdiction) or UNSPECIFIED_STATE
            self._insert_client(db, name or client_id, state, client_id=client_id)
            return client_id, state
        state = normalize_state(jurisdiction) or UNSPECIFIED_STATE
        cid = self._insert_client(db, name or "Synthetic Client", state)
        return cid, state

    def _ensure_client_link(self) -> None:
        """Add the matters.client_id column if absent and backfill orphan matters.

        Backfill is 1:1 and lossless: each pre-Client matter gets its own Client
        (named after the matter) carrying the matter's old free-text jurisdiction
        normalized to a state code (or ``UNSPECIFIED_STATE`` when unrecognizable).
        When the original free-text can't be normalized it is preserved verbatim in
        an audit-log entry so nothing is lost.
        """
        with self.connect() as db:
            cols = [row[1] for row in db.execute("PRAGMA table_info(matters)")]
            if "client_id" not in cols:
                db.execute(
                    "ALTER TABLE matters ADD COLUMN client_id TEXT REFERENCES clients(id)"
                )
            orphans = db.execute(
                "SELECT id, name, jurisdiction FROM matters "
                "WHERE client_id IS NULL OR client_id=''"
            ).fetchall()
            for matter_id, name, juris in orphans:
                state = normalize_state(juris) or UNSPECIFIED_STATE
                cid = self._insert_client(db, name or matter_id, state)
                original = (juris or "").strip()
                if original and normalize_state(original) is None:
                    db.execute(
                        "INSERT INTO audit_logs(matter_id,event_type,details,timestamp) "
                        "VALUES(?,?,?,?)",
                        (
                            matter_id,
                            "client_backfill",
                            f"linked to client {cid}; original jurisdiction text "
                            f"preserved: {original!r}",
                            self.now(),
                        ),
                    )
                db.execute(
                    "UPDATE matters SET client_id=?, jurisdiction=? WHERE id=?",
                    (cid, state, matter_id),
                )

    def create_client(
        self, display_name: str, state: str, client_id: str | None = None
    ) -> str:
        """Create a Client with a validated jurisdiction. Returns the client id."""
        code = validate_state(state)
        name = (display_name or "").strip()
        if not name:
            raise ValueError("Client display name is required.")
        with self.connect() as db:
            cid = self._insert_client(db, name, code, client_id)
        self.log("client_created", None, f"{cid}: {name} [{code}]")
        return cid

    def list_clients(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM clients ORDER BY display_name")
            ]

    def get_client(self, client_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM clients WHERE id=?", (client_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_client_state(self, client_id: str, state: str) -> None:
        """Change a client's jurisdiction; matters derive from it so they stay in
        sync (the stored matters.jurisdiction is also refreshed defensively)."""
        code = validate_state(state)
        with self.connect() as db:
            cur = db.execute(
                "UPDATE clients SET state=?, updated_at=? WHERE id=?",
                (code, self.now(), client_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Unknown client_id: {client_id!r}")
            db.execute(
                "UPDATE matters SET jurisdiction=? WHERE client_id=?", (code, client_id)
            )
        self.log("client_updated", None, f"{client_id}: state -> {code}")

    def log(self, event_type: str, matter_id: str | None = None, details: str = "") -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO audit_logs(matter_id,event_type,details,timestamp) VALUES(?,?,?,?)",
                (matter_id, event_type, details, self.now()),
            )

    # Read matters with jurisdiction DERIVED from the linked client (never the
    # possibly-stale stored column) so a Task's jurisdiction can't diverge from
    # its client's. ``client_name`` is surfaced for the UI.
    _MATTER_SELECT = (
        "SELECT m.id, m.name, m.description, m.created_at, m.client_id, "
        "COALESCE(c.state, m.jurisdiction) AS jurisdiction, "
        "c.display_name AS client_name "
        "FROM matters m LEFT JOIN clients c ON c.id = m.client_id"
    )

    def create_matter(
        self,
        name: str,
        description: str = "",
        jurisdiction: str = "",
        client_id: str | None = None,
    ) -> str:
        matter_id = f"MAT-{uuid.uuid4().hex[:10].upper()}"
        with self.connect() as db:
            resolved_client, state = self._resolve_client(
                db, client_id, name, jurisdiction
            )
            db.execute(
                "INSERT INTO matters(id,name,description,jurisdiction,created_at,client_id) "
                "VALUES(?,?,?,?,?,?)",
                (
                    matter_id,
                    name.strip(),
                    description.strip(),
                    state,
                    self.now(),
                    resolved_client,
                ),
            )
        self.log("matter_created", matter_id, name)
        return matter_id

    def list_matters(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    f"{self._MATTER_SELECT} ORDER BY m.created_at DESC"
                )
            ]

    def get_matter(self, matter_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                f"{self._MATTER_SELECT} WHERE m.id=?", (matter_id,)
            ).fetchone()
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

    # --- Client policy library (RAYAAAA-245, Phase B) ----------------------
    #
    # A Client's uploaded HR/company policy corpus is stored and indexed apart
    # from any single Task's documents. These rows are keyed by ``client_id``;
    # the on-disk index is ``PolicyLibraryIndex(client_id)`` (client-scoped
    # Chroma store). Policy chunks reuse the ``SourceChunk`` shape where the
    # ``matter_id`` field carries the *client id* (that is what was passed to
    # ``extract_document`` and what salts the source-reference), keeping one
    # ingestion/chunk model rather than a parallel one.

    def add_policy_document(self, client_id: str, name: str, path: Path) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO client_policy_documents(client_id,name,path,file_type,size,uploaded_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(client_id,name) DO UPDATE SET
                   path=excluded.path,size=excluded.size,uploaded_at=excluded.uploaded_at,
                   processed_at=NULL""",
                (client_id, name, str(path), path.suffix.lower(), path.stat().st_size, self.now()),
            )
        self.log("policy_uploaded", None, f"{client_id}: {name}")

    def list_policy_documents(self, client_id: str) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM client_policy_documents WHERE client_id=? ORDER BY name",
                    (client_id,),
                )
            ]

    def replace_policy_document_chunks(
        self, client_id: str, document_name: str, chunks: Iterable[SourceChunk]
    ) -> None:
        chunk_list = list(chunks)
        with self.connect() as db:
            db.execute(
                "DELETE FROM policy_chunks WHERE client_id=? AND document_name=?",
                (client_id, document_name),
            )
            db.executemany(
                """INSERT INTO policy_chunks(source_ref,client_id,document_name,file_type,page,row_number,section,text)
                   VALUES(?,?,?,?,?,?,?,?)""",
                [
                    (
                        c.source_ref, client_id, c.document_name, c.file_type,
                        c.page, c.row, c.section, c.text,
                    )
                    for c in chunk_list
                ],
            )
            db.execute(
                "UPDATE client_policy_documents SET processed_at=? WHERE client_id=? AND name=?",
                (self.now(), client_id, document_name),
            )
        self.log("policy_processed", None, f"{client_id}/{document_name}: {len(chunk_list)} chunks")

    def get_policy_chunks(self, client_id: str) -> list[SourceChunk]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM policy_chunks WHERE client_id=? ORDER BY document_name,page,row_number",
                (client_id,),
            )
            return [
                SourceChunk(
                    matter_id=row["client_id"], document_name=row["document_name"],
                    file_type=row["file_type"], page=row["page"], row=row["row_number"],
                    section=row["section"], text=row["text"], source_ref=row["source_ref"],
                )
                for row in rows
            ]

    def delete_policy_document(self, client_id: str, document_name: str) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM policy_chunks WHERE client_id=? AND document_name=?",
                (client_id, document_name),
            )
            db.execute(
                "DELETE FROM client_policy_documents WHERE client_id=? AND name=?",
                (client_id, document_name),
            )
        self.log("policy_deleted", None, f"{client_id}/{document_name}")

    def get_audit_log(self, matter_id: str) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM audit_logs WHERE matter_id=? ORDER BY timestamp DESC",
                    (matter_id,),
                )
            ]
