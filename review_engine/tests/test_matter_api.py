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
