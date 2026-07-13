"""Tests for the per-client policy library + client-scoped retrieval (RAYAAAA-245).

The headline requirement is the CROSS-CLIENT ISOLATION boundary: a Task under
Client X must never be able to retrieve Client Y's policy content, and that must
be enforced by SCOPING (which store is even queried), not by post-filtering. Two
levels prove it:

* ``test_composed_retriever_never_queries_other_clients`` — with injected index
  factories (no chromadb needed) we record every (id) that was searched and show
  Client Y's store is never touched for a Client X Task query.
* ``test_real_chroma_cross_client_isolation`` — the real on-disk
  ``PolicyLibraryIndex`` / ``EvidenceIndex`` stores are built for X and Y and the
  composed retriever is exercised end-to-end; Y's uniquely-worded policy is not
  retrievable from the X Task even when queried for verbatim.

Synthetic / owner-internal data only (Phase 4 gate: RAYAAAA-196/198).
"""
from __future__ import annotations

import pytest

from review_engine.app.policy_audit import checklist_from_policies
from review_engine.app.retrieval import compose_rows, make_client_scoped_retriever
from review_engine.audits.database import ReviewDatabase
from review_engine.clients.policy_library import (
    POLICY_COLLECTION_PREFIX,
    PolicyLibraryIndex,
)
from review_engine.extraction.models import SourceChunk, source_reference


# --- helpers ----------------------------------------------------------------


def _row(source_ref, text, distance=0.1):
    return {
        "source_ref": source_ref,
        "text": text,
        "citation": f"doc.txt ({source_ref})",
        "distance": distance,
    }


class _FakeRegistry:
    """Registry of per-id fake indexes that record which id gets queried."""

    def __init__(self):
        self.store: dict[str, list[dict]] = {}
        self.queried: list[str] = []

    def add(self, key: str, rows: list[dict]):
        self.store.setdefault(key, []).extend(rows)

    def factory(self):
        registry = self

        class _Idx:
            def __init__(self, key: str):
                self.key = key

            def search(self, query: str, limit: int) -> list[dict]:
                registry.queried.append(self.key)
                rows = registry.store.get(self.key, [])
                terms = set(query.lower().split())
                scored = sorted(
                    rows,
                    key=lambda r: -len(terms & set(r["text"].lower().split())),
                )
                out = []
                for position, row in enumerate(scored[:limit]):
                    clone = dict(row)
                    clone["distance"] = 0.1 * position
                    out.append(clone)
                return out

        return _Idx


class _FakeDB:
    def __init__(self, matter_to_client: dict[str, str]):
        self._map = matter_to_client

    def get_matter(self, matter_id: str):
        return {"client_id": self._map.get(matter_id)}


def _chunk(owner_id: str, document_name: str, text: str, ordinal: int) -> SourceChunk:
    return SourceChunk(
        matter_id=owner_id,
        document_name=document_name,
        file_type="txt",
        text=text,
        source_ref=source_reference(owner_id, document_name, section="body", ordinal=ordinal),
        section="body",
    )


# --- composition + provenance -----------------------------------------------


def test_compose_rows_merges_by_distance_and_tags_origin():
    task = [_row("SRC-T1", "task alpha", distance=0.5)]
    policy = [_row("SRC-P1", "policy beta", distance=0.1)]
    merged = compose_rows(task, policy, limit=8)
    # Sorted by distance ascending; both origins tagged.
    assert [r["source_ref"] for r in merged] == ["SRC-P1", "SRC-T1"]
    assert {r["source_ref"]: r["origin"] for r in merged} == {
        "SRC-P1": "policy",
        "SRC-T1": "task",
    }


def test_compose_rows_respects_limit():
    task = [_row(f"SRC-T{i}", "t", distance=i) for i in range(5)]
    policy = [_row(f"SRC-P{i}", "p", distance=i + 0.5) for i in range(5)]
    assert len(compose_rows(task, policy, limit=3)) == 3


# --- cross-client isolation (structural, no chromadb) -----------------------


def test_composed_retriever_never_queries_other_clients():
    registry = _FakeRegistry()
    registry.add("MAT-X", [_row("SRC-TASK", "termination notice period")])
    registry.add("CLI-X", [_row("SRC-XPOL", "client x vacation policy thirty days")])
    # Client Y's policy exists in the store but belongs to a different client.
    registry.add("CLI-Y", [_row("SRC-YPOL", "client y secret non-compete clause")])

    db = _FakeDB({"MAT-X": "CLI-X"})
    retriever = make_client_scoped_retriever(
        db,
        task_index_factory=registry.factory(),
        policy_index_factory=registry.factory(),
    )
    rows = retriever("MAT-X", "vacation policy non-compete", 8)
    refs = {r["source_ref"] for r in rows}

    # The Task's own docs and its linked client's policies are retrievable...
    assert "SRC-TASK" in refs
    assert "SRC-XPOL" in refs
    # ...but Client Y's policy is NOT — and its store was never even queried.
    assert "SRC-YPOL" not in refs
    assert "CLI-Y" not in registry.queried
    assert set(registry.queried) == {"MAT-X", "CLI-X"}


def test_composed_retriever_without_linked_client_uses_task_only():
    registry = _FakeRegistry()
    registry.add("MAT-Z", [_row("SRC-TASK", "some clause")])
    db = _FakeDB({"MAT-Z": None})
    retriever = make_client_scoped_retriever(
        db,
        task_index_factory=registry.factory(),
        policy_index_factory=registry.factory(),
    )
    rows = retriever("MAT-Z", "clause", 8)
    assert [r["source_ref"] for r in rows] == ["SRC-TASK"]
    assert registry.queried == ["MAT-Z"]  # no policy store touched


# --- structural namespace isolation -----------------------------------------


def test_policy_index_namespaces_are_distinct(tmp_path):
    x = PolicyLibraryIndex("CLI-X", root=tmp_path)
    y = PolicyLibraryIndex("CLI-Y", root=tmp_path)
    # Physically separate per-client stores + the dedicated policy prefix.
    assert x.root != y.root
    assert x.collection_prefix == POLICY_COLLECTION_PREFIX == "policy"
    # A policy index is namespaced apart from a Task index even for the same id.
    from review_engine.evidence.index import EvidenceIndex

    task = EvidenceIndex("CLI-X", root=tmp_path)
    assert task.collection_prefix == "matter"
    assert task.collection_prefix != x.collection_prefix


# --- policy-derived checklist (SCOPE 3) -------------------------------------


def test_checklist_from_policies_is_grounded_per_document():
    chunks = [
        _chunk("CLI-X", "leave_policy.txt", "Employees accrue 30 days of annual leave.", 0),
        _chunk("CLI-X", "leave_policy.txt", "Unused leave does not roll over.", 1),
        _chunk("CLI-X", "conduct.txt", "No harassment is tolerated under any circumstances.", 2),
    ]
    checklist = checklist_from_policies(chunks)
    ids = {item["id"] for item in checklist}
    assert ids == {"policy::leave_policy.txt", "policy::conduct.txt"}
    leave = next(i for i in checklist if i["id"] == "policy::leave_policy.txt")
    # The query is derived from the client's own policy text — no fabrication.
    assert "annual leave" in leave["query"]
    assert leave["label"] == "Client policy: leave_policy.txt"


def test_checklist_from_policies_is_bounded():
    chunks = [_chunk("CLI-X", f"doc{i}.txt", f"policy text {i}", i) for i in range(50)]
    assert len(checklist_from_policies(chunks, max_items=5)) == 5


def test_checklist_from_empty_library_is_empty():
    assert checklist_from_policies([]) == []


# --- DB persistence ----------------------------------------------------------


def test_policy_document_persistence_roundtrip(tmp_path):
    db = ReviewDatabase(tmp_path / "review.sqlite3")
    client_id = db.create_client("Synthetic Client", "CA")

    policy_file = tmp_path / "handbook.txt"
    policy_file.write_text("Synthetic handbook content.", encoding="utf-8")
    db.add_policy_document(client_id, "handbook.txt", policy_file)

    docs = db.list_policy_documents(client_id)
    assert [d["name"] for d in docs] == ["handbook.txt"]
    assert docs[0]["processed_at"] is None

    chunks = [_chunk(client_id, "handbook.txt", "Vacation is 30 days.", 0)]
    db.replace_policy_document_chunks(client_id, "handbook.txt", chunks)
    stored = db.get_policy_chunks(client_id)
    assert [c.source_ref for c in stored] == [chunks[0].source_ref]
    # The chunk is keyed to the client id (reusing the SourceChunk model).
    assert stored[0].matter_id == client_id
    assert db.list_policy_documents(client_id)[0]["processed_at"] is not None

    db.delete_policy_document(client_id, "handbook.txt")
    assert db.list_policy_documents(client_id) == []
    assert db.get_policy_chunks(client_id) == []


# --- real chromadb end-to-end isolation -------------------------------------


def test_real_chroma_cross_client_isolation(tmp_path):
    pytest.importorskip("chromadb")
    from review_engine.evidence.index import EvidenceIndex

    policy_root = tmp_path / "policy"
    task_root = tmp_path / "task"

    # Client X policy library, Client Y policy library (uniquely worded), and a
    # Task that belongs to Client X.
    x_policy = PolicyLibraryIndex("CLI-X", root=policy_root)
    x_policy.build([_chunk("CLI-X", "x_leave.txt", "Client X grants thirty vacation days annually.", 0)])
    y_policy = PolicyLibraryIndex("CLI-Y", root=policy_root)
    y_policy.build(
        [_chunk("CLI-Y", "y_secret.txt", "Client Y imposes a zephyrquux non-compete of five years.", 0)]
    )
    task = EvidenceIndex("MAT-X", root=task_root)
    task.build([_chunk("MAT-X", "offer.txt", "The offer references the vacation policy.", 0)])

    db = _FakeDB({"MAT-X": "CLI-X"})
    retriever = make_client_scoped_retriever(
        db,
        task_index_factory=lambda mid: EvidenceIndex(mid, root=task_root),
        policy_index_factory=lambda cid: PolicyLibraryIndex(cid, root=policy_root),
    )

    # Sanity: Client Y's store really does contain the secret (it is retrievable
    # from Y's own index) — so the isolation below is scoping, not absence.
    assert any(r["source_ref"] for r in y_policy.search("zephyrquux non-compete", 5))

    # Query the X Task for Y's uniquely-worded secret: it must not surface.
    rows = retriever("MAT-X", "zephyrquux non-compete five years", 8)
    texts = " ".join(r["text"].lower() for r in rows)
    assert "zephyrquux" not in texts

    # The X client's OWN policy is retrievable from the X Task, tagged as policy.
    rows = retriever("MAT-X", "vacation days annually", 8)
    origins = {r["origin"] for r in rows}
    assert "policy" in origins
    assert any("thirty vacation days" in r["text"].lower() for r in rows)
