"""RAYAAAA-196: verify a client erasure wipes a matter from all four stores.

Mirrors the RAYAAAA-181/182 '0/0/0 residual' verification: seed a synthetic
matter across sqlite + uploads + index + reports, erase it, and assert nothing
is left behind — while a SECOND matter is fully preserved (no over-deletion).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from review_engine.privacy import erasure


def _seed_matter(root: Path, matter_id: str) -> None:
    # sqlite rows across every per-matter table
    db_path = root / "review_engine.sqlite3"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS matters(id TEXT PRIMARY KEY, matter_id TEXT, name TEXT);
        CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY, matter_id TEXT, name TEXT);
        CREATE TABLE IF NOT EXISTS chunks(source_ref TEXT PRIMARY KEY, matter_id TEXT, text TEXT);
        CREATE TABLE IF NOT EXISTS entities(id INTEGER PRIMARY KEY, matter_id TEXT, source_ref TEXT, value TEXT);
        CREATE TABLE IF NOT EXISTS findings(id INTEGER PRIMARY KEY, matter_id TEXT, title TEXT);
        CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY, matter_id TEXT, event_type TEXT);
        """
    )
    con.execute("INSERT INTO matters(id, matter_id, name) VALUES(?,?,?)", (matter_id, matter_id, "syn"))
    con.execute("INSERT INTO documents(matter_id, name) VALUES(?,?)", (matter_id, "offer.pdf"))
    con.execute("INSERT INTO chunks(source_ref, matter_id, text) VALUES(?,?,?)", (f"{matter_id}#1", matter_id, "secret salary 90000"))
    con.execute("INSERT INTO entities(matter_id, source_ref, value) VALUES(?,?,?)", (matter_id, f"{matter_id}#1", "Jane Doe"))
    con.execute("INSERT INTO findings(matter_id, title) VALUES(?,?)", (matter_id, "flag"))
    con.execute("INSERT INTO audit_logs(matter_id, event_type) VALUES(?,?)", (matter_id, "file_uploaded"))
    con.commit()
    con.close()

    # raw upload
    up = root / "uploads" / matter_id
    up.mkdir(parents=True, exist_ok=True)
    (up / "offer.pdf").write_bytes(b"PII: Jane Doe SSN 123-45-6789")

    # chroma index dir (simulate PersistentClient tree)
    idx = root / "indexes" / matter_id
    idx.mkdir(parents=True, exist_ok=True)
    (idx / "chroma.sqlite3").write_bytes(b"embedding-vectors-derived-from-pii")

    # a persisted report (defensive path)
    (root / "matters").mkdir(parents=True, exist_ok=True)
    (root / "matters" / f"{matter_id}_review_report.pdf").write_bytes(b"%PDF report with PII")


def _residual_rows(db_path: Path, matter_id: str) -> int:
    con = sqlite3.connect(db_path)
    total = 0
    for t in ("matters", "documents", "chunks", "entities", "findings", "audit_logs"):
        total += con.execute(f"SELECT COUNT(*) FROM {t} WHERE matter_id=?", (matter_id,)).fetchone()[0]
    con.close()
    return total


def test_erase_matter_zero_residual(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(erasure, "UPLOADS_DIR", root / "uploads")
    monkeypatch.setattr(erasure, "INDEXES_DIR", root / "indexes")
    monkeypatch.setattr(erasure, "PROCESSED_DIR", root / "processed")
    monkeypatch.setattr(erasure, "MATTERS_DIR", root / "matters")
    db_path = root / "review_engine.sqlite3"

    target = "MAT-DEADBEEF01"
    keep = "MAT-KEEPALIVE9"
    _seed_matter(root, target)
    _seed_matter(root, keep)

    report = erasure.erase_matter(target, database_path=db_path)

    # 0/0/0/0 residual for the erased matter
    assert report.clean, report.residual_summary()
    assert report.residual_sqlite_rows == 0
    assert report.residual_upload_bytes == 0
    assert report.residual_index_bytes == 0
    assert report.residual_report_bytes == 0
    assert _residual_rows(db_path, target) == 0
    assert not (root / "uploads" / target).exists()
    assert not (root / "indexes" / target).exists()
    assert not (root / "matters" / f"{target}_review_report.pdf").exists()

    # something was actually deleted (not a vacuous pass)
    assert report.sqlite_rows_deleted == 6
    assert report.upload_bytes_deleted > 0
    assert report.index_bytes_deleted > 0
    assert report.report_bytes_deleted > 0

    # the OTHER matter is fully preserved — no over-deletion (IDOR guard)
    assert _residual_rows(db_path, keep) == 6
    assert (root / "uploads" / keep / "offer.pdf").exists()
    assert (root / "indexes" / keep / "chroma.sqlite3").exists()


def test_erase_unknown_matter_is_clean_noop(tmp_path):
    db_path = tmp_path / "empty.sqlite3"
    report = erasure.erase_matter("MAT-DOESNOTEXIST", database_path=db_path)
    assert report.clean
    assert report.sqlite_rows_deleted == 0


def test_erase_rejects_path_traversal(tmp_path):
    import pytest

    for bad in ("../secrets", "a/b", "..", ""):
        with pytest.raises(ValueError):
            erasure.erase_matter(bad, database_path=tmp_path / "x.sqlite3")
