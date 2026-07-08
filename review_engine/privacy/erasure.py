"""GDPR Art.17 erasure for Battalion-Secure's local document store.

This is the Battalion-side half of the RAYAAAA-182 / RAYAAAA-196 erasure fan-out.
A single portal erasure event (main-app ``anonymizeClient``) must reach BOTH the
main app PII and this second store, leaving no orphaned copies behind.

A matter's data lives in exactly four places (see config/settings.py):
  1. sqlite3 rows keyed by matter_id across every table (matters, documents,
     chunks, entities, findings, audit_logs).
  2. Raw uploads under ``data/uploads/<matter_id>/``.
  3. The derived Chroma embedding index under ``data/indexes/<matter_id>/``
     (a PersistentClient directory tree — deleting the tree removes the vectors;
     no chromadb import is required to erase it).
  4. Any per-matter working dirs (``data/processed/<matter_id>``,
     ``data/matters/<matter_id>``) and persisted DOCX/PDF reports named
     ``<matter_id>_review_report.*`` if report persistence is ever enabled.
     (Reports are currently streamed to the browser and NOT persisted
     server-side; this sweep covers them defensively so a future change cannot
     silently leave a residual copy.)

``erase_matter`` is idempotent: erasing an unknown / already-erased matter is a
no-op that still returns a clean 0/0/0 residual report.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from review_engine.config.settings import (
    DATABASE_PATH,
    INDEXES_DIR,
    MATTERS_DIR,
    PROCESSED_DIR,
    UPLOADS_DIR,
)

# Tables that carry a matter_id column and therefore hold per-matter rows.
# `entities` has no matter_id column of its own in older revisions, so it is
# cleared both directly (current schema has matter_id) and via its source_ref
# FK to chunks, to be robust across schema versions.
_MATTER_TABLES = ("audit_logs", "findings", "entities", "chunks", "documents", "matters")


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
class ErasureReport:
    """Residual accounting after an erasure. All-zero == fully erased."""

    matter_id: str
    sqlite_rows_deleted: int = 0
    upload_bytes_deleted: int = 0
    index_bytes_deleted: int = 0
    report_bytes_deleted: int = 0
    residual_sqlite_rows: int = 0
    residual_upload_bytes: int = 0
    residual_index_bytes: int = 0
    residual_report_bytes: int = 0
    notes: list = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return (
            self.residual_sqlite_rows == 0
            and self.residual_upload_bytes == 0
            and self.residual_index_bytes == 0
            and self.residual_report_bytes == 0
        )

    def residual_summary(self) -> str:
        return (
            f"{self.residual_sqlite_rows} sqlite rows / "
            f"{self.residual_upload_bytes} upload bytes / "
            f"{self.residual_index_bytes} index bytes / "
            f"{self.residual_report_bytes} report bytes"
        )


def _count_matter_rows(db: sqlite3.Connection, matter_id: str) -> int:
    total = 0
    for table in _MATTER_TABLES:
        if table == "entities":
            # entities may be linked by matter_id (current schema) and/or only by
            # source_ref -> chunks (older schema). Count each row once via OR.
            try:
                cur = db.execute(
                    "SELECT COUNT(*) FROM entities WHERE matter_id=? OR source_ref IN "
                    "(SELECT source_ref FROM chunks WHERE matter_id=?)",
                    (matter_id, matter_id),
                )
                total += cur.fetchone()[0]
            except sqlite3.OperationalError:
                # No matter_id column: fall back to source_ref linkage only.
                try:
                    cur = db.execute(
                        "SELECT COUNT(*) FROM entities WHERE source_ref IN "
                        "(SELECT source_ref FROM chunks WHERE matter_id=?)",
                        (matter_id,),
                    )
                    total += cur.fetchone()[0]
                except sqlite3.OperationalError:
                    pass
            continue
        try:
            cur = db.execute(f"SELECT COUNT(*) FROM {table} WHERE matter_id=?", (matter_id,))
            total += cur.fetchone()[0]
        except sqlite3.OperationalError:
            # Table or matter_id column absent in this schema revision.
            pass
    return total


def _delete_sqlite_rows(database_path: Path, matter_id: str) -> tuple[int, int]:
    """Delete every row for matter_id. Returns (deleted_before_count, residual_after)."""
    if not Path(database_path).exists():
        return 0, 0
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = OFF")  # we delete children explicitly
    try:
        before = _count_matter_rows(connection, matter_id)
        # Delete entities first (FK child of chunks), then the rest.
        try:
            connection.execute(
                "DELETE FROM entities WHERE source_ref IN "
                "(SELECT source_ref FROM chunks WHERE matter_id=?)",
                (matter_id,),
            )
        except sqlite3.OperationalError:
            pass
        for table in _MATTER_TABLES:
            try:
                connection.execute(f"DELETE FROM {table} WHERE matter_id=?", (matter_id,))
            except sqlite3.OperationalError:
                pass
        connection.commit()
        residual = _count_matter_rows(connection, matter_id)
    finally:
        connection.close()
    return before, residual


def erase_matter(matter_id: str, database_path: Path | str = DATABASE_PATH) -> ErasureReport:
    """Irreversibly remove all data for ``matter_id`` from Battalion's store.

    Idempotent and safe to call for unknown/already-erased matters.
    """
    if not matter_id or "/" in matter_id or "\\" in matter_id or matter_id in (".", ".."):
        raise ValueError(f"Refusing to erase unsafe matter_id: {matter_id!r}")

    report = ErasureReport(matter_id=matter_id)

    # 1. sqlite rows
    deleted_rows, residual_rows = _delete_sqlite_rows(Path(database_path), matter_id)
    report.sqlite_rows_deleted = deleted_rows
    report.residual_sqlite_rows = residual_rows

    # 2. raw uploads
    uploads = UPLOADS_DIR / matter_id
    report.upload_bytes_deleted = _dir_bytes(uploads)
    _rmtree(uploads)
    report.residual_upload_bytes = _dir_bytes(uploads)

    # 3. Chroma embedding index (directory tree — no chromadb import needed)
    index = INDEXES_DIR / matter_id
    report.index_bytes_deleted = _dir_bytes(index)
    _rmtree(index)
    report.residual_index_bytes = _dir_bytes(index)

    # 4. per-matter working dirs + any persisted reports
    report_bytes = 0
    residual_report_bytes = 0
    for working in (PROCESSED_DIR / matter_id, MATTERS_DIR / matter_id):
        report_bytes += _dir_bytes(working)
        _rmtree(working)
        residual_report_bytes += _dir_bytes(working)
    # Persisted report files, if report persistence is ever turned on.
    for base in (MATTERS_DIR, PROCESSED_DIR, UPLOADS_DIR):
        for stray in base.glob(f"{matter_id}_review_report.*"):
            report_bytes += stray.stat().st_size
            stray.unlink(missing_ok=True)
            if stray.exists():
                residual_report_bytes += stray.stat().st_size
    report.report_bytes_deleted = report_bytes
    report.residual_report_bytes = residual_report_bytes

    if not report.clean:
        report.notes.append(f"NON-CLEAN residual: {report.residual_summary()}")
    return report


# ---------------------------------------------------------------------------
# Retention sweep (RAYAAAA-196 AC3, per Counsel/RAYAAAA-195 requirements)
#
# Counsel (RAYAAAA-195, 2026-07-07): Battalion-Secure is a review workspace, not
# the system of record. Required retention: purge a matter on matter CLOSURE with
# a hard idle BACKSTOP of 90 days since last review activity; embeddings purge
# with their source doc (whole-matter erase covers this); reports are never
# persisted in-tool. The purge job is keyed to matter_id + last-activity.
#
# Matter *closure* is a portal/main-app signal and is driven main-app-side via
# the same erasure fan-out (it calls erase_matter). This sweep implements the
# Battalion-side idle backstop so the tool can never become a shadow archive
# even if a closure event is missed.
# ---------------------------------------------------------------------------

RETENTION_IDLE_DAYS = 90  # Counsel/CEO decision, RAYAAAA-195 (2026-07-07)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _last_activity(db: sqlite3.Connection, matter_id: str, created_at: str | None) -> datetime | None:
    """Most recent signal of activity for a matter: newest audit-log entry,
    falling back to the matter's created_at when it has no logged activity."""
    latest = None
    try:
        row = db.execute(
            "SELECT MAX(timestamp) FROM audit_logs WHERE matter_id=?", (matter_id,)
        ).fetchone()
        latest = _parse_ts(row[0]) if row else None
    except sqlite3.OperationalError:
        latest = None
    return latest or _parse_ts(created_at)


@dataclass
class RetentionSweepResult:
    idle_days: int
    scanned: int = 0
    purged: list = field(default_factory=list)          # [(matter_id, ErasureReport)]
    skipped_unclean: list = field(default_factory=list)  # matter_ids whose erase left residual
    now: str = ""

    @property
    def purged_ids(self) -> list:
        return [mid for mid, _ in self.purged]

    def summary(self) -> str:
        return (
            f"retention sweep @ {self.now}: scanned {self.scanned} matters, "
            f"purged {len(self.purged)} idle>{self.idle_days}d "
            f"({', '.join(self.purged_ids) or 'none'}); "
            f"{len(self.skipped_unclean)} non-clean"
        )


def sweep_retention(
    idle_days: int = RETENTION_IDLE_DAYS,
    database_path: Path | str = DATABASE_PATH,
    now: datetime | None = None,
) -> RetentionSweepResult:
    """Erase every matter idle longer than ``idle_days``. Safe to run repeatedly
    (e.g. a daily cron). Uses ``erase_matter`` so each purge is a full 4-store wipe."""
    now = now or datetime.now(timezone.utc)
    result = RetentionSweepResult(idle_days=idle_days, now=now.isoformat())
    db_path = Path(database_path)
    if not db_path.exists():
        return result

    connection = sqlite3.connect(db_path)
    try:
        try:
            matters = connection.execute("SELECT id, created_at FROM matters").fetchall()
        except sqlite3.OperationalError:
            matters = []
        stale: list[str] = []
        for matter_id, created_at in matters:
            result.scanned += 1
            last = _last_activity(connection, matter_id, created_at)
            # No parseable activity timestamp at all -> treat as stale (fail-safe).
            if last is None or (now - last).days >= idle_days:
                stale.append(matter_id)
    finally:
        connection.close()

    for matter_id in stale:
        report = erase_matter(matter_id, database_path=db_path)
        result.purged.append((matter_id, report))
        if not report.clean:
            result.skipped_unclean.append(matter_id)
    return result
