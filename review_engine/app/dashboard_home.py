"""Dashboard landing view (RAYAAAA-263 / owner base44 "Aich-R" demo).

Renders the "Welcome back" home screen: a header + New Request button, four stat
tiles wired to real Task/review counts, a "Start a Review" grid of the six
review-type cards, and a Recent Requests + Admin row. All navigation is
session-state based (``st.session_state['nav']``) so the sidebar shell in
``main.py`` and these cards drive the same single-page router. A review-type card
stashes its ``key`` in ``st.session_state['new_request_type']`` before routing to
the New Request wizard (owned by the sibling issue), which reads it to prefilter.

Pure data derivation lives in :mod:`review_engine.dashboard.home`; this module is
only Streamlit rendering.
"""
from __future__ import annotations

import streamlit as st

from review_engine.app.review_types import REVIEW_TYPES
from review_engine.app.icons import icon as ui_icon
from review_engine.dashboard.home import dashboard_stats, recent_requests

# Human-readable labels + chip colours for the four stat tiles. Colours mirror
# the demo (blue / amber / green / purple) and are semantic, not brand tokens.
_STAT_TILES = [
    ("total", "Total Requests", "document", "#3b82f6"),
    ("in_progress", "In Progress", "clock", "#f59e0b"),
    ("completed", "Completed", "check", "#10b981"),
    ("needs_review", "Needs Review", "alert", "#8b5cf6"),
]

_STATUS_LABELS = {
    "in_progress": "In Progress",
    "completed": "Completed",
    "needs_review": "Needs Review",
}


def _go(nav: str, review_type: str | None = None) -> None:
    """Router helper: set the active view (and optional review-type prefilter).

    The prefilter is written to ``nr_type`` — the exact session key the New Request
    wizard (sibling RAYAAAA-264) reads — so a "Start a Review" card lands the
    wizard prefiltered to that type once both branches are merged.
    """
    st.session_state["nav"] = nav
    if review_type is not None:
        st.session_state["nr_type"] = review_type
    st.rerun()


def render_dashboard_home(svc) -> None:
    # --- Header row: welcome + top-right New Request -------------------------
    head_left, head_right = st.columns([4, 1])
    with head_left:
        st.markdown(
            "<h1 class='dash-welcome'>Welcome to RAYSERR Lens</h1>"
            "<p class='dash-subtitle'>Here's what's happening with your reviews</p>",
            unsafe_allow_html=True,
        )
    with head_right:
        st.write("")
        if st.button("＋  New Request", type="primary", use_container_width=True, key="dash_new_request"):
            _go("new_request")

    # --- Stat tiles ----------------------------------------------------------
    stats = dashboard_stats(svc.db)
    tile_cols = st.columns(4)
    for col, (key, label, icon, color) in zip(tile_cols, _STAT_TILES):
        with col:
            with st.container(border=True):
                st.markdown(
                    f"<div class='stat-chip' style='background:{color}1a;color:{color};'>{ui_icon(icon)}</div>"
                    f"<div class='stat-value'>{stats.get(key, 0)}</div>"
                    f"<div class='stat-label'>{label}</div>",
                    unsafe_allow_html=True,
                )

    st.write("")

    # --- Start a Review: 6 review-type cards --------------------------------
    with st.container(border=True):
        title_col, browse_col = st.columns([4, 1])
        with title_col:
            st.markdown("<h3 class='panel-title'>Start a Review</h3>", unsafe_allow_html=True)
        with browse_col:
            st.write("")
            if st.button("Browse all", key="start_browse_all", use_container_width=True):
                _go("new_request")
        grid = st.columns(3)
        for idx, rt in enumerate(REVIEW_TYPES):
            with grid[idx % 3]:
                with st.container(border=True):
                    st.markdown(
                        f"<div class='review-card'>"
                        f"<span class='review-chip' style='background:{rt.color}1a;color:{rt.color};'>{ui_icon(rt.icon)}</span>"
                        f"<span class='review-text'><span class='review-title'>{rt.title}</span>"
                        f"<span class='review-sub'>{rt.subtitle}</span></span></div>",
                        unsafe_allow_html=True,
                    )
                    if st.button("Start  →", key=f"start_{rt.key}", use_container_width=True):
                        _go("new_request", review_type=rt.key)

    st.write("")

    # --- Recent Requests + Admin row ----------------------------------------
    recent_col, admin_col = st.columns([2, 1])
    with recent_col:
        with st.container(border=True):
            rr_title, rr_link = st.columns([3, 1])
            with rr_title:
                st.markdown("<h3 class='panel-title'>Recent Requests</h3>", unsafe_allow_html=True)
            with rr_link:
                st.write("")
                if st.button("View all", key="recent_view_all", use_container_width=True):
                    _go("my_requests")
            recents = recent_requests(svc.db, limit=5)
            if not recents:
                st.markdown(
                    "<p class='empty-note'>No requests yet. Submit your first one!</p>",
                    unsafe_allow_html=True,
                )
            else:
                for row in recents:
                    st.markdown(
                        f"<div class='recent-row'><span class='recent-name'>{row['name']}</span>"
                        f"<span class='recent-meta'>{row['client_name']} · "
                        f"{_STATUS_LABELS.get(row['status'], row['status'])}</span></div>",
                        unsafe_allow_html=True,
                    )
    with admin_col:
        with st.container(border=True):
            st.markdown("<h3 class='panel-title'>Admin</h3>", unsafe_allow_html=True)
            if st.button("Review Queue", key="admin_review_queue", use_container_width=True):
                _go("review_queue")
            if st.button("Policy Library", key="admin_policy_library", use_container_width=True):
                _go("policy_library")
