"""RAYAAAA-264: headless render smoke for the two redesigned views.

Uses Streamlit's AppTest to run the real app script with a live ScriptRunContext
(unlike a bare import) and asserts the New Request wizard and Policy Library page
render without raising. Local-only; no browser needed.

RAYAAAA-263: the app shell replaced the sidebar "View" radio with session-state
button nav, so navigation here clicks the sidebar nav button by key instead of
setting a radio.
"""
from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "app" / "main.py")

# Map the legacy view names these tests were written against to the new nav keys.
_NAV_KEY = {
    "New Request": "nav_new_request",
    "Client policy library": "nav_policy_library",
}


def _goto(at: AppTest, view: str) -> AppTest:
    nav_key = _NAV_KEY.get(view, view)
    for b in at.button:
        if b.key == nav_key:
            b.click().run()
            return at
    raise AssertionError(f"nav button {nav_key!r} for view {view!r} not found")


def _run(view: str) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    _goto(at, view)
    return at


def test_new_request_step1_renders():
    at = _run("New Request")
    assert not at.exception, at.exception
    # Step 1 heading + the six selectable review-type buttons are present.
    titles = " ".join(m.value for m in at.title)
    assert "New Review Request" in titles
    labels = [b.label for b in at.button]
    # Each of the 6 cards renders a Select button, plus the Continue CTA.
    assert labels.count("Select") == 6


def test_policy_library_renders():
    at = _run("Client policy library")
    assert not at.exception, at.exception
    titles = " ".join(m.value for m in at.title)
    assert "Policy Library" in titles


def test_new_request_select_type_advances_cta(monkeypatch):
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    _goto(at, "New Request")
    # Click the first "Select" -> the CTA becomes "Continue with Legal Case Analysis".
    for b in at.button:
        if b.label == "Select":
            b.click().run()
            break
    assert not at.exception, at.exception
    assert any("Continue with Legal Case Analysis" in b.label for b in at.button)
