"""RAYAAAA-244 (Phase A): first-class Client + Task<->Client link + client-level
jurisdiction. SYNTHETIC-only — no real client PII (standing Phase 4 gate,
RAYAAAA-196/198).

Covers: the Client model, the validated jurisdiction list, the Task<->Client link
(exactly one client per Task), jurisdiction derivation (a Task's jurisdiction
comes from its client and can't diverge), the identity-reuse hook (a matter
created with an explicit client_id materializes/links a client keyed by that same
identity — the erasure-fanout client id), and the backfill migration that links
pre-existing (pre-Client) matters with zero data loss.
"""

from __future__ import annotations

import sqlite3

import pytest

from review_engine.audits.database import ReviewDatabase
from review_engine.clients import jurisdictions as J


def _db(tmp_path) -> ReviewDatabase:
    return ReviewDatabase(tmp_path / "review_engine.sqlite3")


# --- controlled jurisdiction list -----------------------------------------


def test_jurisdiction_normalization_and_validation():
    assert J.normalize_state("ca") == "CA"
    assert J.normalize_state("California") == "CA"
    assert J.normalize_state("  new york ") == "NY"
    assert J.normalize_state("US") == J.UNSPECIFIED_STATE
    assert J.normalize_state("Freedonia") is None
    assert J.normalize_state("") is None
    assert J.normalize_state(None) is None
    assert J.is_valid_state("tx") is True
    assert J.is_valid_state("ZZ") is False
    # validate_state raises on junk, returns canonical code otherwise.
    assert J.validate_state("Texas") == "TX"
    with pytest.raises(ValueError):
        J.validate_state("not-a-state")


def test_unspecified_state_is_a_valid_member():
    assert J.UNSPECIFIED_STATE in J.JURISDICTION_CHOICES
    assert J.is_valid_state(J.UNSPECIFIED_STATE)


# --- Client model ----------------------------------------------------------


def test_create_client_persists_validated_state(tmp_path):
    db = _db(tmp_path)
    cid = db.create_client("Synthetic Co", "california")
    assert cid.startswith("CLI-")
    client = db.get_client(cid)
    assert client["display_name"] == "Synthetic Co"
    assert client["state"] == "CA"  # normalized to a canonical code
    assert client["created_at"] and client["updated_at"]
    assert [c["id"] for c in db.list_clients()] == [cid]


def test_create_client_rejects_bad_state_and_blank_name(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(ValueError):
        db.create_client("X", "Atlantis")
    with pytest.raises(ValueError):
        db.create_client("   ", "CA")


def test_update_client_state_propagates_to_matters(tmp_path):
    db = _db(tmp_path)
    cid = db.create_client("Synthetic Co", "CA")
    mid = db.create_matter("Case A", client_id=cid)
    assert db.get_matter(mid)["jurisdiction"] == "CA"
    db.update_client_state(cid, "New York")
    # Derived jurisdiction follows the client — never diverges.
    assert db.get_matter(mid)["jurisdiction"] == "NY"
    with pytest.raises(ValueError):
        db.update_client_state("CLI-nope", "CA")


# --- Task <-> Client link --------------------------------------------------


def test_matter_links_to_exactly_one_client(tmp_path):
    db = _db(tmp_path)
    cid = db.create_client("Synthetic Co", "TX")
    mid = db.create_matter("Case A", client_id=cid)
    matter = db.get_matter(mid)
    assert matter["client_id"] == cid
    assert matter["client_name"] == "Synthetic Co"
    assert matter["jurisdiction"] == "TX"  # derived from the client


def test_matter_without_client_gets_a_one_to_one_client(tmp_path):
    # Legacy / producer path (no client_id): a 1:1 synthetic client is created so
    # the "exactly one client per Task" invariant always holds.
    db = _db(tmp_path)
    mid = db.create_matter("Orphan Case", jurisdiction="florida")
    matter = db.get_matter(mid)
    assert matter["client_id"]
    assert matter["jurisdiction"] == "FL"
    client = db.get_client(matter["client_id"])
    assert client["state"] == "FL"


def test_matter_with_unknown_jurisdiction_defaults_to_unspecified(tmp_path):
    db = _db(tmp_path)
    mid = db.create_matter("Weird Case", jurisdiction="somewhere")
    assert db.get_matter(mid)["jurisdiction"] == J.UNSPECIFIED_STATE


# --- identity reuse (erasure-fanout client id) -----------------------------


def test_explicit_client_id_materializes_client_keyed_by_that_identity(tmp_path):
    # The erasure fan-out already groups a client's matters by a client identity.
    # Passing that same id here materializes/links a Battalion client row keyed by
    # it — no parallel identity store.
    db = _db(tmp_path)
    portal_id = "client_portal_abc123"
    mid = db.create_matter("Synthetic matter", jurisdiction="NY", client_id=portal_id)
    matter = db.get_matter(mid)
    assert matter["client_id"] == portal_id
    assert db.get_client(portal_id)["state"] == "NY"
    # A second matter with the SAME identity reuses the same client (grouping).
    mid2 = db.create_matter("Second matter", client_id=portal_id)
    assert db.get_matter(mid2)["client_id"] == portal_id
    assert len([c for c in db.list_clients() if c["id"] == portal_id]) == 1


# --- backfill migration (zero data loss) -----------------------------------


def _seed_pre_client_schema(path, rows):
    """Create the OLD matters schema (no clients table, no client_id column) and
    insert rows so we can prove the migration links them without loss."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE matters (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
            jurisdiction TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE documents (id INTEGER PRIMARY KEY, matter_id TEXT, name TEXT);
        CREATE TABLE audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, matter_id TEXT,
            event_type TEXT NOT NULL, details TEXT, timestamp TEXT NOT NULL
        );
        """
    )
    for mid, name, juris in rows:
        con.execute(
            "INSERT INTO matters(id,name,description,jurisdiction,created_at) VALUES(?,?,?,?,?)",
            (mid, name, "desc", juris, "2026-07-01T00:00:00+00:00"),
        )
        # a child document per matter — must survive the migration untouched.
        con.execute(
            "INSERT INTO documents(matter_id,name) VALUES(?,?)", (mid, f"{mid}.pdf")
        )
    con.commit()
    con.close()


def test_migration_backfills_pre_client_matters_without_data_loss(tmp_path):
    db_path = tmp_path / "review_engine.sqlite3"
    _seed_pre_client_schema(
        db_path,
        [
            ("MAT-AAAA", "CA case", "California"),
            ("MAT-BBBB", "blank juris", ""),
            ("MAT-CCCC", "free text", "Somewhere County, TX region"),
        ],
    )

    # Opening the DB runs initialize() -> _ensure_client_link() (the migration).
    db = ReviewDatabase(db_path)

    matters = {m["id"]: m for m in db.list_matters()}
    # Zero data loss: every matter still present, each documents row intact.
    assert set(matters) == {"MAT-AAAA", "MAT-BBBB", "MAT-CCCC"}
    for mid in matters:
        assert db.list_documents(mid) == db.list_documents(mid)  # no error
        con = sqlite3.connect(db_path)
        assert con.execute(
            "SELECT COUNT(*) FROM documents WHERE matter_id=?", (mid,)
        ).fetchone()[0] == 1
        con.close()

    # Each matter now resolves to exactly one client, with a derived jurisdiction.
    for mid in matters:
        assert matters[mid]["client_id"]
    assert matters["MAT-AAAA"]["jurisdiction"] == "CA"  # recognized -> normalized
    assert matters["MAT-BBBB"]["jurisdiction"] == J.UNSPECIFIED_STATE  # blank
    assert matters["MAT-CCCC"]["jurisdiction"] == J.UNSPECIFIED_STATE  # unparseable

    # The unparseable original free-text is preserved in the audit log (no loss).
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    notes = [
        dict(r)
        for r in con.execute(
            "SELECT * FROM audit_logs WHERE matter_id='MAT-CCCC' AND event_type='client_backfill'"
        )
    ]
    con.close()
    assert notes, "expected a backfill provenance note"
    assert "Somewhere County, TX region" in notes[0]["details"]


def test_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "review_engine.sqlite3"
    _seed_pre_client_schema(db_path, [("MAT-AAAA", "CA case", "California")])
    ReviewDatabase(db_path)  # first migration
    clients_after_first = len(ReviewDatabase(db_path).list_clients())
    # Re-opening must NOT create duplicate clients or re-link already-linked matters.
    clients_after_second = len(ReviewDatabase(db_path).list_clients())
    assert clients_after_first == clients_after_second == 1
