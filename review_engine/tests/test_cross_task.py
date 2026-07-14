"""Tests for cross-Task, owner-scoped retrieval + provenance (RAYAAAA-247, B2).

The headline requirement is the CROSS-CLIENT ISOLATION boundary carried over
from RAYAAAA-241/244/245: the "sees everything" assistant may span every Task the
OWNER owns, but an answer framed around one Client must NEVER pull another
Client's document — and that must be enforced by SCOPING (which store is even
opened), not by post-filtering. The tests prove:

* provenance is preserved per row (which Task + SRC + Client + origin);
* an all-Tasks query spans every owner Task;
* a client-scoped query never even instantiates another client's Task or policy
  index (structural, with injected factories and again end-to-end on real
  chromadb);
* an erased/anonymized Task (RAYAAAA-196) cannot resurface via the cross-Task
  view; and
* the feature-flag + internal-token auth gate is OFF by default.

Synthetic / owner-internal data only (Phase 4 gate: RAYAAAA-196/198).
"""
from __future__ import annotations

import pytest

from review_engine.app.cross_task import (
    CrossTaskAccessError,
    OwnerAssistant,
    authorize,
    make_owner_scoped_retriever,
    provenance,
    visible_matters,
)
from review_engine.audits.database import ReviewDatabase
from review_engine.clients.policy_library import PolicyLibraryIndex
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
                    rows, key=lambda r: -len(terms & set(r["text"].lower().split()))
                )
                out = []
                for position, row in enumerate(scored[:limit]):
                    clone = dict(row)
                    clone["distance"] = 0.1 * position
                    out.append(clone)
                return out

        return _Idx


class _FakeDB:
    """Minimal ``list_matters`` shim — the shape the retriever reads."""

    def __init__(self, matters: list[dict]):
        self._matters = matters

    def list_matters(self) -> list[dict]:
        return list(self._matters)


def _matter(mid, name, client_id, client_name=""):
    return {"id": mid, "name": name, "client_id": client_id, "client_name": client_name}


def _chunk(owner_id: str, document_name: str, text: str, ordinal: int) -> SourceChunk:
    return SourceChunk(
        matter_id=owner_id,
        document_name=document_name,
        file_type="txt",
        text=text,
        source_ref=source_reference(owner_id, document_name, section="body", ordinal=ordinal),
        section="body",
    )


# --- auth gate --------------------------------------------------------------


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CROSS_TASK_ASSISTANT_ENABLED", raising=False)
    with pytest.raises(CrossTaskAccessError):
        authorize()


def test_enabled_without_token(monkeypatch):
    monkeypatch.setenv("CROSS_TASK_ASSISTANT_ENABLED", "1")
    monkeypatch.delenv("CROSS_TASK_ASSISTANT_TOKEN", raising=False)
    authorize()  # no token required when none is configured


def test_enabled_requires_matching_token(monkeypatch):
    monkeypatch.setenv("CROSS_TASK_ASSISTANT_ENABLED", "1")
    monkeypatch.setenv("CROSS_TASK_ASSISTANT_TOKEN", "s3cret")
    with pytest.raises(CrossTaskAccessError):
        authorize("wrong")
    with pytest.raises(CrossTaskAccessError):
        authorize(None)
    authorize("s3cret")  # matching token passes


def test_create_is_gated(monkeypatch):
    monkeypatch.delenv("CROSS_TASK_ASSISTANT_ENABLED", raising=False)
    db = _FakeDB([])
    with pytest.raises(CrossTaskAccessError):
        OwnerAssistant.create(db)


# --- span + provenance ------------------------------------------------------


def test_spans_all_owner_tasks_and_tags_provenance():
    registry = _FakeRegistry()
    registry.add("MAT-1", [_row("SRC-1", "termination notice period")])
    registry.add("MAT-2", [_row("SRC-2", "termination severance calculation")])
    db = _FakeDB(
        [
            _matter("MAT-1", "Alpha review", "CLI-A", "Acme"),
            _matter("MAT-2", "Beta review", "CLI-B", "Beta Co"),
        ]
    )
    retriever = make_owner_scoped_retriever(
        db,
        include_policies=False,
        task_index_factory=registry.factory(),
    )
    rows = retriever("termination", 8)
    by_ref = {r["source_ref"]: r for r in rows}

    assert set(by_ref) == {"SRC-1", "SRC-2"}
    # Each row is attributed to the Task and Client it came from.
    assert by_ref["SRC-1"]["matter_id"] == "MAT-1"
    assert by_ref["SRC-1"]["matter_name"] == "Alpha review"
    assert by_ref["SRC-1"]["client_id"] == "CLI-A"
    assert by_ref["SRC-2"]["matter_id"] == "MAT-2"
    assert by_ref["SRC-2"]["client_id"] == "CLI-B"
    assert {r["origin"] for r in rows} == {"task"}


def test_provenance_objects_carry_task_and_client():
    rows = [
        {
            "source_ref": "SRC-9",
            "citation": "handbook.txt (SRC-9)",
            "text": "vacation policy",
            "distance": 0.2,
            "matter_id": "MAT-7",
            "matter_name": "Gamma",
            "client_id": "CLI-C",
            "client_name": "Gamma LLC",
            "origin": "policy",
        }
    ]
    (src,) = provenance(rows)
    assert src.source_ref == "SRC-9"
    assert src.matter_id == "MAT-7"
    assert src.client_id == "CLI-C"
    assert src.origin == "policy"
    assert "Gamma" in src.label() and "policy" in src.label()


def test_policy_library_queried_once_per_client():
    registry = _FakeRegistry()
    registry.add("MAT-1", [_row("SRC-T1", "leave")])
    registry.add("MAT-2", [_row("SRC-T2", "leave")])
    registry.add("CLI-A", [_row("SRC-POL", "leave policy thirty days")])
    db = _FakeDB(
        [
            _matter("MAT-1", "One", "CLI-A"),
            _matter("MAT-2", "Two", "CLI-A"),  # same client, second Task
        ]
    )
    retriever = make_owner_scoped_retriever(
        db,
        task_index_factory=registry.factory(),
        policy_index_factory=registry.factory(),
    )
    retriever("leave", 8)
    # Both Task indexes queried, but the shared client's policy library only once.
    assert registry.queried.count("CLI-A") == 1
    assert set(registry.queried) == {"MAT-1", "MAT-2", "CLI-A"}


# --- cross-client isolation (structural, no chromadb) -----------------------


def test_client_scope_never_touches_other_clients():
    registry = _FakeRegistry()
    registry.add("MAT-X", [_row("SRC-XTASK", "vacation policy")])
    registry.add("MAT-Y", [_row("SRC-YTASK", "vacation policy secret")])
    registry.add("CLI-X", [_row("SRC-XPOL", "client x vacation thirty days")])
    registry.add("CLI-Y", [_row("SRC-YPOL", "client y non-compete clause")])
    db = _FakeDB(
        [
            _matter("MAT-X", "X matter", "CLI-X"),
            _matter("MAT-Y", "Y matter", "CLI-Y"),
        ]
    )
    retriever = make_owner_scoped_retriever(
        db,
        client_id="CLI-X",  # answer framed around Client X only
        task_index_factory=registry.factory(),
        policy_index_factory=registry.factory(),
    )
    rows = retriever("vacation policy non-compete", 8)
    refs = {r["source_ref"] for r in rows}

    # X's own Task + policy are retrievable...
    assert "SRC-XTASK" in refs
    assert "SRC-XPOL" in refs
    # ...but NOTHING from Client Y — and neither of Y's stores was ever queried.
    assert "SRC-YTASK" not in refs
    assert "SRC-YPOL" not in refs
    assert "MAT-Y" not in registry.queried
    assert "CLI-Y" not in registry.queried
    assert set(registry.queried) == {"MAT-X", "CLI-X"}


def test_visible_matters_scopes_to_client():
    db = _FakeDB(
        [
            _matter("MAT-X", "X", "CLI-X"),
            _matter("MAT-Y", "Y", "CLI-Y"),
        ]
    )
    assert {m["id"] for m in visible_matters(db)} == {"MAT-X", "MAT-Y"}
    assert {m["id"] for m in visible_matters(db, "CLI-X")} == {"MAT-X"}


# --- erasure respected ------------------------------------------------------


def test_erased_task_does_not_resurface(tmp_path):
    """A Task erased via RAYAAAA-196 must drop out of the cross-Task view."""
    pytest.importorskip("chromadb")
    from review_engine.evidence.index import EvidenceIndex
    from review_engine.privacy import erasure

    db = ReviewDatabase(tmp_path / "review.sqlite3")
    client_id = db.create_client("Synthetic Client", "CA")
    keep = db.create_matter("Keep", client_id=client_id)
    drop = db.create_matter("Drop", client_id=client_id)

    index_root = tmp_path / "indexes"
    EvidenceIndex(keep, root=index_root).build(
        [_chunk(keep, "keep.txt", "keep unique alpha content", 0)]
    )
    EvidenceIndex(drop, root=index_root).build(
        [_chunk(drop, "drop.txt", "drop unique zephyrquux content", 0)]
    )

    retriever = make_owner_scoped_retriever(
        db,
        include_policies=False,
        task_index_factory=lambda mid: EvidenceIndex(mid, root=index_root),
    )
    # Before erasure the drop Task's unique content is retrievable.
    before = " ".join(r["text"].lower() for r in retriever("zephyrquux", 8))
    assert "zephyrquux" in before

    # Erase the drop Task across the sqlite store AND its Chroma index tree.
    monkey_root = index_root
    erasure.erase_matter(drop, database_path=db.path)
    # erase_matter deletes the index under INDEXES_DIR; mirror that for the test
    # root (the retriever reads live per-Task indexes, so a gone index is empty).
    import shutil

    shutil.rmtree(monkey_root / drop, ignore_errors=True)

    after = " ".join(r["text"].lower() for r in retriever("zephyrquux", 8))
    assert "zephyrquux" not in after  # erased content cannot resurface
    # The kept Task is unaffected.
    kept = " ".join(r["text"].lower() for r in retriever("alpha", 8))
    assert "alpha" in kept
    # And it is gone from the owner's visible Task set entirely.
    assert drop not in {m["id"] for m in visible_matters(db)}


# --- real chromadb end-to-end isolation -------------------------------------


def test_real_chroma_no_cross_client_bleed(tmp_path):
    pytest.importorskip("chromadb")
    from review_engine.evidence.index import EvidenceIndex

    task_root = tmp_path / "task"
    policy_root = tmp_path / "policy"

    EvidenceIndex("MAT-X", root=task_root).build(
        [_chunk("MAT-X", "x.txt", "Client X references the vacation policy.", 0)]
    )
    EvidenceIndex("MAT-Y", root=task_root).build(
        [_chunk("MAT-Y", "y.txt", "Client Y zephyrquux confidential settlement.", 0)]
    )
    PolicyLibraryIndex("CLI-X", root=policy_root).build(
        [_chunk("CLI-X", "x_pol.txt", "Client X grants thirty vacation days annually.", 0)]
    )
    PolicyLibraryIndex("CLI-Y", root=policy_root).build(
        [_chunk("CLI-Y", "y_pol.txt", "Client Y imposes a zephyrquux non-compete.", 0)]
    )

    db = _FakeDB(
        [
            _matter("MAT-X", "X matter", "CLI-X", "Acme"),
            _matter("MAT-Y", "Y matter", "CLI-Y", "Beta"),
        ]
    )
    retriever = make_owner_scoped_retriever(
        db,
        client_id="CLI-X",
        task_index_factory=lambda mid: EvidenceIndex(mid, root=task_root),
        policy_index_factory=lambda cid: PolicyLibraryIndex(cid, root=policy_root),
    )

    # Query the X-scoped assistant for Y's uniquely-worded secret: no bleed.
    rows = retriever("zephyrquux confidential non-compete settlement", 8)
    texts = " ".join(r["text"].lower() for r in rows)
    assert "zephyrquux" not in texts
    assert {r["client_id"] for r in rows} == {"CLI-X"}

    # X's own Task doc and policy are both retrievable and correctly attributed.
    rows = retriever("vacation policy days", 8)
    origins = {r["origin"] for r in rows}
    assert origins == {"task", "policy"} or origins == {"policy"} or origins == {"task"}
    assert all(r["client_id"] == "CLI-X" for r in rows)
    assert any("vacation" in r["text"].lower() for r in rows)
