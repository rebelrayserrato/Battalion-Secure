"""Tests for the RAYAAAA-210 matter-API sidecar.

Exercises the internal create-matter endpoint end-to-end against a temporary
sqlite store: create a matter -> a Battalion matter id comes back -> the matters
row is present in the shared store (which is exactly what the erasure fan-out and
the Streamlit UI read).
"""

from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from review_engine.audits.database import ReviewDatabase


def _client(monkeypatch, tmp_path, token: str | None = None):
    db_path = tmp_path / "review_engine.sqlite3"
    # Point the shared DATABASE_PATH at a temp file BEFORE the app module builds
    # its module-level ReviewDatabase().
    monkeypatch.setattr(
        "review_engine.config.settings.DATABASE_PATH", db_path, raising=False
    )
    if token is not None:
        monkeypatch.setenv("MATTER_API_TOKEN", token)
    else:
        monkeypatch.delenv("MATTER_API_TOKEN", raising=False)

    import review_engine.api.matter_api as matter_api

    importlib.reload(matter_api)
    matter_api._db = ReviewDatabase(db_path)
    return TestClient(matter_api.app), ReviewDatabase(db_path)


BASE = "/admin/review-engine/api"


def test_health(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    res = client.get(f"{BASE}/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_create_matter_returns_id_and_persists_row(monkeypatch, tmp_path):
    client, db = _client(monkeypatch, tmp_path)

    res = client.post(
        f"{BASE}/matters",
        json={"name": "Synthetic Client · General", "description": "synthetic e2e"},
    )
    assert res.status_code == 201, res.text
    payload = res.json()
    matter_id = payload["matter_id"]
    assert matter_id.startswith("MAT-")
    assert payload["name"] == "Synthetic Client · General"
    assert payload["created_at"]

    # The row is present in the SAME store the Streamlit UI + erasure tooling read.
    row = db.get_matter(matter_id)
    assert row is not None
    assert row["name"] == "Synthetic Client · General"
    assert [m["id"] for m in db.list_matters()] == [matter_id]


def test_blank_name_rejected(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    res = client.post(f"{BASE}/matters", json={"name": "   "})
    assert res.status_code == 422


def test_token_gate(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, token="s3cret")
    # Missing/wrong token -> 403.
    assert client.post(f"{BASE}/matters", json={"name": "X"}).status_code == 403
    assert (
        client.post(
            f"{BASE}/matters", json={"name": "X"}, headers={"X-Internal-Token": "nope"}
        ).status_code
        == 403
    )
    # Correct token -> created.
    ok = client.post(
        f"{BASE}/matters", json={"name": "X"}, headers={"X-Internal-Token": "s3cret"}
    )
    assert ok.status_code == 201, ok.text


# --- RAYAAAA-212: internal HTTP erase endpoint -----------------------------


def test_erase_matter_clean_then_gone(monkeypatch, tmp_path):
    client, db = _client(monkeypatch, tmp_path)

    matter_id = client.post(
        f"{BASE}/matters", json={"name": "Synthetic · Erase me"}
    ).json()["matter_id"]
    assert db.get_matter(matter_id) is not None

    res = client.delete(f"{BASE}/matters/{matter_id}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["matter_id"] == matter_id
    assert body["clean"] is True
    assert body["residual_sqlite_rows"] == 0
    assert body["sqlite_rows_deleted"] >= 1  # at least the matters row

    # The matter is gone from the SAME shared store the fan-out reads.
    assert db.get_matter(matter_id) is None
    assert [m["id"] for m in db.list_matters()] == []


def test_erase_unknown_matter_is_idempotent_noop(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    # Erasing a never-seen (or already-erased) matter is a clean 0/0 no-op → 200,
    # so the fan-out's retry never wedges on an id Battalion never held.
    res = client.delete(f"{BASE}/matters/MAT-doesnotexist")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["clean"] is True
    assert body["sqlite_rows_deleted"] == 0
    assert body["residual_sqlite_rows"] == 0


def test_erase_residual_fails_loud_500(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    import review_engine.api.matter_api as matter_api
    from review_engine.privacy.erasure import ErasureReport

    def _fake_erase(matter_id, database_path=None):
        # Simulate a wipe that left residual behind (e.g. a locked file / partial
        # failure) — the endpoint MUST fail loud so the fan-out retries.
        return ErasureReport(matter_id=matter_id, residual_sqlite_rows=2)

    monkeypatch.setattr(matter_api, "erase_matter", _fake_erase)
    res = client.delete(f"{BASE}/matters/MAT-partial")
    assert res.status_code == 500, res.text
    detail = res.json()["detail"]
    assert detail["clean"] is False
    assert detail["residual_sqlite_rows"] == 2


def test_erase_token_gate(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, token="s3cret")
    # No/invalid token -> 403, same posture as the create endpoint.
    assert client.delete(f"{BASE}/matters/MAT-x").status_code == 403
    assert (
        client.delete(
            f"{BASE}/matters/MAT-x", headers={"X-Internal-Token": "nope"}
        ).status_code
        == 403
    )
    ok = client.delete(f"{BASE}/matters/MAT-x", headers={"X-Internal-Token": "s3cret"})
    assert ok.status_code == 200, ok.text
