"""RAYAAAA-303: per-client storage ACL + client-policy erasure reach.

The headline requirement is the STORAGE-LAYER TENANCY BOUNDARY: a per-client
policy path is derived solely from a *validated* ``client_id`` and can never
escape its client namespace, the P1 (RAYAAAA-302) session claim is enforced
per-tenant at the seam, and a client erasure reaches the per-client policy store
(folder + index + rows) with zero residual and no over-deletion.

Synthetic / owner-internal data only (Phase-4 gate: RAYAAAA-297 / RAYAAAA-301).
"""
from __future__ import annotations

import sqlite3

import pytest

from review_engine.clients import storage
from review_engine.clients.storage import (
    ClientAccessError,
    ClientScope,
    CrossClientAccessError,
    assert_within,
    client_policy_upload_dir,
    require_client,
    validate_client_id,
)
from review_engine.privacy import client_erasure


# --- client_id validation ---------------------------------------------------


@pytest.mark.parametrize(
    "cid", ["CLI-ABCDEF0123", "client_a", "Client-1.v2", "a", "A" * 64]
)
def test_validate_accepts_safe_ids(cid):
    assert validate_client_id(cid) == cid


@pytest.mark.parametrize(
    "cid",
    [
        "",
        ".",
        "..",
        "../other-client",
        "a/b",
        "a\\b",
        "/etc/passwd",
        "cli\x00id",
        "A" * 65,
        "has space",
        "weird*char",
    ],
)
def test_validate_rejects_unsafe_ids(cid):
    with pytest.raises(ClientAccessError):
        validate_client_id(cid)


def test_validate_never_rewrites():
    # A silently-rewritten id could collide two tenants — validation rejects,
    # never sanitises.
    with pytest.raises(ClientAccessError):
        validate_client_id("a b/c")


# --- path derivation + containment ------------------------------------------


def test_assert_within_rejects_escape(tmp_path):
    root = tmp_path / "policy_uploads"
    assert assert_within(root, root / "CLI-X") == (root / "CLI-X").resolve()
    with pytest.raises(ClientAccessError):
        assert_within(root, root / ".." / "elsewhere")


def test_upload_dir_is_per_client_and_created(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "POLICY_UPLOADS_DIR", tmp_path / "policy_uploads")
    a = client_policy_upload_dir("CLI-A")
    b = client_policy_upload_dir("CLI-B")
    assert a.is_dir() and b.is_dir()
    assert a != b
    assert a.name == "CLI-A" and a.parent == (tmp_path / "policy_uploads").resolve()


def test_upload_dir_rejects_traversal_id(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "POLICY_UPLOADS_DIR", tmp_path / "policy_uploads")
    with pytest.raises(ClientAccessError):
        client_policy_upload_dir("../escape")
    # nothing was created outside the root
    assert not (tmp_path / "escape").exists()


# --- P1 session-claim seam --------------------------------------------------


def test_scope_guard_allows_same_client():
    scope = ClientScope.for_client("CLI-A")
    assert scope.guard("CLI-A") == "CLI-A"


def test_scope_guard_rejects_cross_client():
    scope = ClientScope.for_client("CLI-A")
    with pytest.raises(CrossClientAccessError):
        scope.guard("CLI-B")


def test_require_client_no_scope_just_validates():
    assert require_client(None, "CLI-A") == "CLI-A"
    with pytest.raises(ClientAccessError):
        require_client(None, "../x")


def test_require_client_with_scope_enforces_tenant():
    scope = ClientScope.for_client("CLI-A")
    assert require_client(scope, "CLI-A") == "CLI-A"
    with pytest.raises(CrossClientAccessError):
        require_client(scope, "CLI-B")


def test_scope_for_client_validates_at_construction():
    with pytest.raises(ClientAccessError):
        ClientScope.for_client("../evil")


# --- erasure reach ----------------------------------------------------------


def _seed_client_policy(root, db_path, client_id: str) -> None:
    """Seed a client's policy uploads folder + index dir + sqlite rows."""
    uploads = root / "policy_uploads" / client_id
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / "handbook.txt").write_text("synthetic policy for " + client_id)
    index = root / "policy_indexes" / client_id
    index.mkdir(parents=True, exist_ok=True)
    (index / "chroma.sqlite3").write_text("fake index bytes")

    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS client_policy_documents(
            id INTEGER PRIMARY KEY, client_id TEXT NOT NULL, name TEXT);
        CREATE TABLE IF NOT EXISTS policy_chunks(
            source_ref TEXT PRIMARY KEY, client_id TEXT NOT NULL, text TEXT);
        """
    )
    con.execute(
        "INSERT INTO client_policy_documents(client_id, name) VALUES(?,?)",
        (client_id, "handbook.txt"),
    )
    con.execute(
        "INSERT INTO policy_chunks(source_ref, client_id, text) VALUES(?,?,?)",
        (f"{client_id}#1", client_id, "synthetic"),
    )
    con.commit()
    con.close()


def _policy_rows(db_path, client_id: str) -> int:
    con = sqlite3.connect(db_path)
    n = con.execute(
        "SELECT COUNT(*) FROM policy_chunks WHERE client_id=?", (client_id,)
    ).fetchone()[0]
    n += con.execute(
        "SELECT COUNT(*) FROM client_policy_documents WHERE client_id=?", (client_id,)
    ).fetchone()[0]
    con.close()
    return n


def test_erase_client_policy_zero_residual_no_over_deletion(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(client_erasure, "POLICY_UPLOADS_DIR", root / "policy_uploads")
    monkeypatch.setattr(client_erasure, "POLICY_INDEXES_DIR", root / "policy_indexes")
    db_path = root / "review_engine.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    _seed_client_policy(root, db_path, "CLI-A")
    _seed_client_policy(root, db_path, "CLI-B")

    report = client_erasure.erase_client_policy_store("CLI-A", database_path=db_path)

    assert report.clean, report.residual_summary()
    assert report.sqlite_rows_deleted == 2
    assert report.upload_bytes_deleted > 0
    assert report.index_bytes_deleted > 0
    # A is gone from disk + db
    assert not (root / "policy_uploads" / "CLI-A").exists()
    assert not (root / "policy_indexes" / "CLI-A").exists()
    assert _policy_rows(db_path, "CLI-A") == 0
    # B is fully preserved (no over-deletion)
    assert (root / "policy_uploads" / "CLI-B" / "handbook.txt").exists()
    assert (root / "policy_indexes" / "CLI-B").exists()
    assert _policy_rows(db_path, "CLI-B") == 2


def test_erase_unknown_client_is_clean_noop(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(client_erasure, "POLICY_UPLOADS_DIR", root / "policy_uploads")
    monkeypatch.setattr(client_erasure, "POLICY_INDEXES_DIR", root / "policy_indexes")
    db_path = root / "empty.sqlite3"
    report = client_erasure.erase_client_policy_store("CLI-NOPE", database_path=db_path)
    assert report.clean
    assert report.sqlite_rows_deleted == 0
    assert report.upload_bytes_deleted == 0
    assert report.index_bytes_deleted == 0


def test_erase_is_idempotent(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(client_erasure, "POLICY_UPLOADS_DIR", root / "policy_uploads")
    monkeypatch.setattr(client_erasure, "POLICY_INDEXES_DIR", root / "policy_indexes")
    db_path = root / "review_engine.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_client_policy(root, db_path, "CLI-A")
    first = client_erasure.erase_client_policy_store("CLI-A", database_path=db_path)
    second = client_erasure.erase_client_policy_store("CLI-A", database_path=db_path)
    assert first.clean and second.clean
    assert second.sqlite_rows_deleted == 0
    assert second.upload_bytes_deleted == 0


def test_erase_rejects_unsafe_client_id(tmp_path):
    with pytest.raises(ClientAccessError):
        client_erasure.erase_client_policy_store(
            "../CLI-A", database_path=tmp_path / "x.sqlite3"
        )
