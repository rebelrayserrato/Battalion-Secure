"""Tests for the jurisdiction-scoped law reference library + law-grounded
retrieval (RAYAAAA-251, Phase C).

The two headline, counsel-binding requirements each get a dedicated failure-mode
test:

* CROSS-JURISDICTION ISOLATION (AC C) — a Task whose client is in State A can
  never retrieve State B's law text, enforced by SCOPING (which partition is even
  queried), not post-filtering. Proven structurally (injected factories, no
  chromadb) AND end-to-end against the real on-disk Chroma stores.
* NO-CITATION-WITHOUT-RETRIEVED-CHUNK (AC E) — a statute/section citation not
  backed by a retrieved law chunk is redacted to "not in reference library".

Plus: mandatory provenance (AC B), provenance stamp format (AC F), the verbatim
disclaimer (AC G), and law-corpus exclusion from client-data erasure (AC H).

Synthetic / owner-internal data only (Phase 4 gate: RAYAAAA-196/198).
"""
from __future__ import annotations

import pytest

from review_engine.audits.database import ReviewDatabase
from review_engine.extraction.models import SourceChunk, source_reference
from review_engine.law.grounding import (
    LawGroundedAnswerer,
    compose_law_grounded_rows,
    make_law_grounded_retriever,
)
from review_engine.law.library import (
    FEDERAL_JURISDICTION,
    LAW_COLLECTION_PREFIX,
    LAW_DISCLAIMER,
    LawLibraryIndex,
    LawProvenance,
    NOT_IN_LIBRARY,
    citation_stamp,
    enforce_law_citation_guardrail,
    resolve_law_jurisdictions,
    validate_law_jurisdiction,
)


# --- helpers ----------------------------------------------------------------


def _row(source_ref, text, distance=0.1, origin=None, **extra):
    row = {
        "source_ref": source_ref,
        "text": text,
        "citation": f"doc.txt ({source_ref})",
        "section": extra.pop("section", ""),
        "distance": distance,
    }
    if origin:
        row["origin"] = origin
    row.update(extra)
    return row


def _chunk(owner_id: str, document_name: str, text: str, ordinal: int, section: str = "body") -> SourceChunk:
    return SourceChunk(
        matter_id=owner_id,
        document_name=document_name,
        file_type="txt",
        text=text,
        source_ref=source_reference(owner_id, document_name, section=section, ordinal=ordinal),
        section=section,
    )


class _Registry:
    """Per-key fake indexes that record which key was queried."""

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
                scored = sorted(rows, key=lambda r: -len(terms & set(r["text"].lower().split())))
                out = []
                for position, row in enumerate(scored[:limit]):
                    clone = dict(row)
                    clone["distance"] = 0.1 * position
                    out.append(clone)
                return out

        return _Idx


class _FakeDB:
    def __init__(self, matter):
        self._matter = matter

    def get_matter(self, matter_id):
        return dict(self._matter)


# --- jurisdiction resolution (AC C) -----------------------------------------


def test_resolve_law_jurisdictions_state_plus_federal():
    assert resolve_law_jurisdictions("CA") == [FEDERAL_JURISDICTION, "CA"]


def test_resolve_law_jurisdictions_unspecified_is_federal_only():
    # The Phase-A "US" sentinel (unspecified/federal) resolves to federal only.
    assert resolve_law_jurisdictions("US") == [FEDERAL_JURISDICTION]
    assert resolve_law_jurisdictions("") == [FEDERAL_JURISDICTION]
    assert resolve_law_jurisdictions(None) == [FEDERAL_JURISDICTION]


def test_validate_law_jurisdiction():
    assert validate_law_jurisdiction("federal") == "federal"
    assert validate_law_jurisdiction("ca") == "CA"
    assert validate_law_jurisdiction("California") == "CA"
    assert validate_law_jurisdiction("US") == "federal"
    with pytest.raises(ValueError):
        validate_law_jurisdiction("Ontario")


# --- provenance (AC B / AC F) -----------------------------------------------


def test_provenance_requires_all_fields():
    ok = LawProvenance("Cornell LII", "https://law.cornell.edu", "2024 ed.", "2026-07-13")
    assert ok.validate() is ok
    for missing in ("source_name", "source_url", "effective", "retrieved"):
        kwargs = {
            "source_name": "S", "source_url": "U", "effective": "V", "retrieved": "D",
        }
        kwargs[missing] = "   "
        with pytest.raises(ValueError):
            LawProvenance(**kwargs).validate()


def test_citation_stamp_format():
    stamp = citation_stamp("eCFR", "2026-07-13", "2024 ed.")
    assert stamp == "[Source: eCFR, retrieved 2026-07-13, effective 2024 ed.]"
    # LawProvenance.stamp() agrees.
    prov = LawProvenance("eCFR", "https://ecfr.gov", "2024 ed.", "2026-07-13")
    assert prov.stamp() == stamp


def test_provenance_metadata_keys_are_prefixed():
    prov = LawProvenance("eCFR", "https://ecfr.gov", "2024 ed.", "2026-07-13")
    md = prov.as_metadata()
    assert md == {
        "law_source_name": "eCFR",
        "law_source_url": "https://ecfr.gov",
        "law_effective": "2024 ed.",
        "law_retrieved": "2026-07-13",
    }


# --- disclaimer (AC G) ------------------------------------------------------


def test_disclaimer_is_verbatim_from_memo():
    # Exact opening + closing sentences from RAYAAAA-243 memo §2.
    assert LAW_DISCLAIMER.startswith(
        "Not legal advice. This response is an automated document-review aid."
    )
    assert LAW_DISCLAIMER.rstrip().endswith(
        "Verify all cited authority against the official source before relying on it."
    )
    assert "does not create an attorney-client relationship" in LAW_DISCLAIMER


def test_answer_always_carries_disclaimer():
    registry = _Registry()
    registry.add("federal", [_row("SRC-F1", "29 U.S.C. § 206 minimum wage", origin=None)])
    db = _FakeDB({"client_id": None, "jurisdiction": "US"})
    retriever = make_law_grounded_retriever(
        db,
        task_index_factory=registry.factory(),
        policy_index_factory=registry.factory(),
        law_index_factory=registry.factory(),
    )

    class _NoModel:
        def available(self):
            return False

    answerer = LawGroundedAnswerer(retriever=retriever, connector=_NoModel())
    result = answerer.answer("MAT-1", "minimum wage")
    assert result.disclaimer == LAW_DISCLAIMER


# --- no-citation-without-retrieved-chunk guardrail (AC E) -------------------


def test_guardrail_redacts_unbacked_citation():
    law_rows = [_row("SRC-L1", "Labor Code Section 512 requires a meal period.", section="512")]
    answer = "The rule is in § 512, but see also § 999.99 for exceptions."
    sanitized, redacted = enforce_law_citation_guardrail(answer, law_rows)
    # § 512 is backed by retrieved text and survives; § 999.99 is fabricated.
    assert "512" in sanitized
    assert "999.99" not in sanitized
    assert NOT_IN_LIBRARY in sanitized
    assert any("999.99" in r for r in redacted)


def test_guardrail_redacts_when_no_law_retrieved():
    # No law chunk retrieved at all -> every citation is unbacked.
    sanitized, redacted = enforce_law_citation_guardrail("See 29 C.F.R. § 541.100.", [])
    assert "541.100" not in sanitized
    assert NOT_IN_LIBRARY in sanitized
    assert redacted


def test_guardrail_preserves_backed_usc_citation():
    law_rows = [_row("SRC-L1", "29 U.S.C. 206 sets the federal minimum wage.", section="206")]
    sanitized, redacted = enforce_law_citation_guardrail("Under 29 U.S.C. § 206 ...", law_rows)
    assert "206" in sanitized
    assert redacted == []


def test_guardrail_digit_boundary_prevents_false_positive():
    # "§ 12" must NOT be considered backed merely because "512" contains "12".
    law_rows = [_row("SRC-L1", "Section 512 only.", section="512")]
    sanitized, _ = enforce_law_citation_guardrail("See § 12 here.", law_rows)
    assert NOT_IN_LIBRARY in sanitized


def test_law_grounded_answer_sanitizes_model_output():
    registry = _Registry()
    registry.add("CA", [_row("SRC-CA1", "California Labor Code Section 512 meal period.", origin=None, section="512")])
    registry.add("federal", [])
    db = _FakeDB({"client_id": None, "jurisdiction": "CA"})
    retriever = make_law_grounded_retriever(
        db,
        task_index_factory=lambda k: _Registry().factory()(k),
        policy_index_factory=lambda k: _Registry().factory()(k),
        law_index_factory=registry.factory(),
    )

    class _FakeModel:
        def available(self):
            return True

        def generate(self, prompt):
            # Model tries to smuggle in a fabricated citation from "training data".
            return "Meal periods are governed by § 512 and also § 12345 (invented)."

    answerer = LawGroundedAnswerer(retriever=retriever, connector=_FakeModel())
    result = answerer.answer("MAT-1", "meal period", limit=8)
    assert "512" in result.answer
    assert "12345" not in result.answer
    assert NOT_IN_LIBRARY in result.answer
    assert any("12345" in r for r in result.redacted_citations)


# --- composition (AC D) -----------------------------------------------------


def test_compose_keeps_three_distinct_origins():
    merged = compose_law_grounded_rows(
        [_row("SRC-T", "t", distance=0.5)],
        [_row("SRC-P", "p", distance=0.3)],
        [_row("SRC-L", "l", distance=0.1)],
        limit=8,
    )
    assert [r["source_ref"] for r in merged] == ["SRC-L", "SRC-P", "SRC-T"]
    assert {r["source_ref"]: r["origin"] for r in merged} == {
        "SRC-L": "law", "SRC-P": "policy", "SRC-T": "task",
    }


# --- cross-jurisdiction isolation, structural (AC C) ------------------------


def test_retriever_never_queries_other_states_law():
    registry = _Registry()
    registry.add("MAT-X", [_row("SRC-TASK", "the contract term")])
    registry.add("CLI-X", [_row("SRC-POL", "client policy")])
    registry.add("CA", [_row("SRC-CA", "california meal period law")])
    registry.add("federal", [_row("SRC-FED", "federal flsa overtime")])
    # Texas law exists in the store but the client is in California.
    registry.add("TX", [_row("SRC-TX", "texas zephyrquux statute")])

    db = _FakeDB({"client_id": "CLI-X", "jurisdiction": "CA"})
    retriever = make_law_grounded_retriever(
        db,
        task_index_factory=registry.factory(),
        policy_index_factory=registry.factory(),
        law_index_factory=registry.factory(),
    )
    rows = retriever("MAT-X", "meal period overtime zephyrquux", 8)
    refs = {r["source_ref"] for r in rows}

    assert {"SRC-TASK", "SRC-POL", "SRC-CA", "SRC-FED"} <= refs
    # Texas law is neither retrieved NOR even queried — structural isolation.
    assert "SRC-TX" not in refs
    assert "TX" not in registry.queried
    assert set(registry.queried) == {"MAT-X", "CLI-X", "CA", "federal"}


def test_retriever_federal_only_for_unspecified_client():
    registry = _Registry()
    registry.add("MAT-Z", [_row("SRC-TASK", "clause")])
    registry.add("federal", [_row("SRC-FED", "federal law")])
    registry.add("CA", [_row("SRC-CA", "california law")])
    db = _FakeDB({"client_id": None, "jurisdiction": "US"})
    retriever = make_law_grounded_retriever(
        db,
        task_index_factory=registry.factory(),
        policy_index_factory=registry.factory(),
        law_index_factory=registry.factory(),
    )
    rows = retriever("MAT-Z", "law", 8)
    refs = {r["source_ref"] for r in rows}
    assert "SRC-FED" in refs
    assert "SRC-CA" not in refs
    assert "CA" not in registry.queried


# --- structural namespace isolation -----------------------------------------


def test_law_index_namespaces_are_distinct(tmp_path):
    ca = LawLibraryIndex("CA", root=tmp_path)
    tx = LawLibraryIndex("TX", root=tmp_path)
    assert ca.root != tx.root
    assert ca.collection_prefix == LAW_COLLECTION_PREFIX == "law"
    # A law index is namespaced apart from a Task index and a policy index even
    # if they ever shared an id/root.
    from review_engine.clients.policy_library import PolicyLibraryIndex
    from review_engine.evidence.index import EvidenceIndex

    assert EvidenceIndex("CA", root=tmp_path).collection_prefix == "matter"
    assert PolicyLibraryIndex("CA", root=tmp_path).collection_prefix == "policy"


# --- DB persistence + provenance roundtrip ----------------------------------


def test_law_document_persistence_roundtrip(tmp_path):
    db = ReviewDatabase(tmp_path / "review.sqlite3")
    law_file = tmp_path / "flsa.txt"
    law_file.write_text("29 U.S.C. 206 minimum wage.", encoding="utf-8")
    db.add_law_document(
        "federal", "flsa.txt", law_file,
        source_name="Cornell LII", source_url="https://law.cornell.edu",
        effective="2024 ed.", retrieved="2026-07-13",
    )
    docs = db.list_law_documents("federal")
    assert [d["name"] for d in docs] == ["flsa.txt"]
    assert docs[0]["source_name"] == "Cornell LII"
    assert db.list_law_jurisdictions() == ["federal"]
    prov = db.law_provenance("federal")
    assert prov["flsa.txt"]["effective"] == "2024 ed."

    chunks = [_chunk("federal", "flsa.txt", "29 U.S.C. 206 minimum wage.", 0, section="206")]
    db.replace_law_document_chunks("federal", "flsa.txt", chunks)
    stored = db.get_law_chunks("federal")
    assert [c.source_ref for c in stored] == [chunks[0].source_ref]
    assert stored[0].matter_id == "federal"  # SourceChunk reused, keyed by jurisdiction

    db.delete_law_document("federal", "flsa.txt")
    assert db.list_law_documents("federal") == []
    assert db.get_law_chunks("federal") == []


# --- retention/erasure separation (AC H) ------------------------------------


def test_law_corpus_excluded_from_matter_erasure(tmp_path, monkeypatch):
    from review_engine.privacy import erasure

    root = tmp_path / "data"
    root.mkdir()
    db = ReviewDatabase(root / "review_engine.sqlite3")

    # A client + matter with matter-keyed data.
    client_id = db.create_client("Synthetic Client", "CA")
    matter_id = db.create_matter("Case", client_id=client_id)
    db.replace_document_chunks(
        matter_id, "offer.txt", [_chunk(matter_id, "offer.txt", "secret salary 90000", 0)]
    )

    # Law corpus keyed by jurisdiction (NOT by this matter/client).
    law_file = root / "flsa.txt"
    law_file.write_text("29 U.S.C. 206 minimum wage.", encoding="utf-8")
    db.add_law_document(
        "federal", "flsa.txt", law_file,
        source_name="Cornell LII", source_url="https://law.cornell.edu",
        effective="2024 ed.", retrieved="2026-07-13",
    )
    db.replace_law_document_chunks(
        "federal", "flsa.txt", [_chunk("federal", "flsa.txt", "29 U.S.C. 206.", 0, section="206")]
    )

    monkeypatch.setattr(erasure, "UPLOADS_DIR", root / "uploads")
    monkeypatch.setattr(erasure, "INDEXES_DIR", root / "indexes")
    monkeypatch.setattr(erasure, "PROCESSED_DIR", root / "processed")
    monkeypatch.setattr(erasure, "MATTERS_DIR", root / "matters")

    report = erasure.erase_matter(matter_id, database_path=db.path)

    # The matter's own data is gone...
    assert db.get_matter(matter_id) is None
    assert db.get_chunks(matter_id) == []
    # ...but the law corpus is fully intact (never keyed by matter/client, and
    # not in erasure._MATTER_TABLES).
    assert "law_documents" not in erasure._MATTER_TABLES
    assert "law_chunks" not in erasure._MATTER_TABLES
    assert [d["name"] for d in db.list_law_documents("federal")] == ["flsa.txt"]
    assert len(db.get_law_chunks("federal")) == 1
    assert report.clean


# --- real chromadb end-to-end cross-jurisdiction isolation (AC C) -----------


def test_real_chroma_cross_jurisdiction_isolation(tmp_path):
    pytest.importorskip("chromadb")
    from review_engine.evidence.index import EvidenceIndex

    law_root = tmp_path / "law"
    task_root = tmp_path / "task"

    # California law (uniquely worded), Texas law (uniquely worded), federal law.
    ca = LawLibraryIndex("CA", root=law_root)
    ca.build_with_provenance(
        [_chunk("CA", "ca_labor.txt", "California Labor Code Section 512 grants a meal period.", 0, section="512")],
        {"ca_labor.txt": LawProvenance("CA Leg Info", "https://leginfo.ca.gov", "2024", "2026-07-13")},
    )
    tx = LawLibraryIndex("TX", root=law_root)
    tx.build_with_provenance(
        [_chunk("TX", "tx_stat.txt", "Texas imposes a zephyrquux rule under Section 61.", 0, section="61")],
        {"tx_stat.txt": LawProvenance("TX Stat", "https://statutes.capitol.texas.gov", "2024", "2026-07-13")},
    )
    fed = LawLibraryIndex("federal", root=law_root)
    fed.build_with_provenance(
        [_chunk("federal", "flsa.txt", "29 U.S.C. 206 sets the federal minimum wage.", 0, section="206")],
        {"flsa.txt": LawProvenance("Cornell LII", "https://law.cornell.edu", "2024", "2026-07-13")},
    )
    task = EvidenceIndex("MAT-CA", root=task_root)
    task.build([_chunk("MAT-CA", "contract.txt", "The contract references meal periods.", 0)])

    db = _FakeDB({"client_id": "CLI-CA", "jurisdiction": "CA"})
    retriever = make_law_grounded_retriever(
        db,
        task_index_factory=lambda mid: EvidenceIndex(mid, root=task_root),
        policy_index_factory=lambda cid: EvidenceIndex(cid, root=tmp_path / "policy", collection_prefix="policy"),
        law_index_factory=lambda j: LawLibraryIndex(j, root=law_root),
    )

    # Sanity: Texas's store really contains the secret (retrievable from TX itself).
    assert any(tx.search("zephyrquux", 5))

    # A California Task must not surface Texas's uniquely-worded law.
    rows = retriever("MAT-CA", "zephyrquux rule section 61", 8)
    joined = " ".join(r["text"].lower() for r in rows)
    assert "zephyrquux" not in joined

    # California + federal law IS retrievable, tagged origin=law with provenance.
    rows = retriever("MAT-CA", "meal period minimum wage", 8)
    law_rows = [r for r in rows if r.get("origin") == "law"]
    assert law_rows
    assert any("meal period" in r["text"].lower() for r in law_rows)
    # Provenance rode through on the retrieved law metadata (AC F).
    assert all(r.get("law_source_name") for r in law_rows)
