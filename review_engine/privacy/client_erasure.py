"""GDPR Art.17 erasure for a Client's per-client policy store (RAYAAAA-303).

The matter-keyed erasure (:mod:`review_engine.privacy.erasure`) wipes a single
Task/matter across the four matter stores. A *client* additionally owns a
policy corpus keyed by ``client_id`` (RAYAAAA-245): raw uploads under
``POLICY_UPLOADS_DIR/<client_id>``, a Chroma index under
``POLICY_INDEXES_DIR/<client_id>``, and the ``client_policy_documents`` /
``policy_chunks`` sqlite rows. Those live *outside* the matter identity — a
single matter erase must NOT nuke a client's shared policy library — so they need
their own client-keyed erasure primitive that participates in the fan-out
(RAYAAAA-207) when the *client itself* is anonymised/offboarded.

``erase_client_policy_store`` removes all four so no orphaned client-policy copy
survives client erasure. It is idempotent (erasing an unknown/already-erased
client is a clean 0/0 no-op) and fail-loud (a non-clean residual is recorded on
the report exactly like ``ErasureReport``), and it reuses the RAYAAAA-303
``client_id`` validator so an unsafe id can never drive a delete over an
unintended path.

SYNTHETIC / owner-internal only until the Phase-4 gate.
"""
from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from review_engine.clients.storage import (
    assert_within,
    validate_client_id,
)
from review_engine.config.settings import (
    DATABASE_PATH,
    POLICY_INDEXES_DIR,
    POLICY_UPLOADS_DIR,
)

# Client-policy tables (both carry a ``client_id`` column, RAYAAAA-245 schema).
_POLICY_TABLES = ("policy_chunks", "client_policy_documents")


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


@dataclass
class ClientPolicyErasureReport:
    """Residual accounting after a client-policy erasure. All-zero == fully erased."""

    client_id: str
    sqlite_rows_deleted: int = 0
    upload_bytes_deleted: int = 0
    index_bytes_deleted: int = 0
    residual_sqlite_rows: int = 0
    residual_upload_bytes: int = 0
    residual_index_bytes: int = 0
    notes: list = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return (
            self.residual_sqlite_rows == 0
            and self.residual_upload_bytes == 0
            and self.residual_index_bytes == 0
        )

    def residual_summary(self) -> str:
        return (
            f"{self.residual_sqlite_rows} policy sqlite rows / "
            f"{self.residual_upload_bytes} policy upload bytes / "
            f"{self.residual_index_bytes} policy index bytes"
        )


def _count_policy_rows(db: sqlite3.Connection, client_id: str) -> int:
    total = 0
    for table in _POLICY_TABLES:
        try:
            cur = db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE client_id=?", (client_id,)
            )
            total += cur.fetchone()[0]
        except sqlite3.OperationalError:
            # Table absent in this schema revision.
            pass
    return total


def _delete_policy_rows(database_path: Path, client_id: str) -> tuple[int, int]:
    if not Path(database_path).exists():
        return 0, 0
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        before = _count_policy_rows(connection, client_id)
        for table in _POLICY_TABLES:
            try:
                connection.execute(
                    f"DELETE FROM {table} WHERE client_id=?", (client_id,)
                )
            except sqlite3.OperationalError:
                pass
        connection.commit()
        residual = _count_policy_rows(connection, client_id)
    finally:
        connection.close()
    return before, residual


def erase_client_policy_store(
    client_id: str, database_path: Path | str = DATABASE_PATH
) -> ClientPolicyErasureReport:
    """Irreversibly remove a client's policy corpus. Idempotent + fail-loud.

    Removes the client's raw policy uploads folder, its policy Chroma index, and
    its ``client_policy_documents`` / ``policy_chunks`` sqlite rows. ``client_id``
    is validated (RAYAAAA-303) before any path is touched, so an unsafe id is
    rejected rather than driving a delete over an unintended tree.
    """
    cid = validate_client_id(client_id)
    report = ClientPolicyErasureReport(client_id=cid)

    # 1. sqlite rows (policy documents + chunks)
    deleted_rows, residual_rows = _delete_policy_rows(Path(database_path), cid)
    report.sqlite_rows_deleted = deleted_rows
    report.residual_sqlite_rows = residual_rows

    # 2. raw per-client policy uploads. Assert containment on the derived path
    #    (no mkdir side effect) — never a bare POLICY_UPLOADS_DIR / cid delete.
    uploads = assert_within(POLICY_UPLOADS_DIR, POLICY_UPLOADS_DIR / cid)
    report.upload_bytes_deleted = _dir_bytes(uploads)
    _rmtree(uploads)
    report.residual_upload_bytes = _dir_bytes(uploads)

    # 3. per-client policy Chroma index tree (containment-checked path)
    index = assert_within(POLICY_INDEXES_DIR, POLICY_INDEXES_DIR / cid)
    report.index_bytes_deleted = _dir_bytes(index)
    _rmtree(index)
    report.residual_index_bytes = _dir_bytes(index)

    if not report.clean:
        report.notes.append(f"NON-CLEAN residual: {report.residual_summary()}")
    return report
