from __future__ import annotations

import base64
import os

import streamlit as st

from review_engine.app.dashboard_home import render_dashboard_home
from review_engine.app.icons import icon as ui_icon
from review_engine.app.new_request import render_new_request
from review_engine.app.policy_audit import PolicyAuditor
from review_engine.app.policy_library_view import render_policy_library
from review_engine.app.retrieval import GroundedAnswerer, make_client_scoped_retriever
from review_engine.app.services import ReviewService
from review_engine.clients.jurisdictions import (
    JURISDICTION_CHOICES,
    UNSPECIFIED_STATE,
    state_label,
)
from review_engine.law.grounding import LawGroundedAnswerer, make_law_grounded_retriever
from review_engine.law.library import (
    LAW_JURISDICTION_CHOICES,
    law_jurisdiction_label,
    resolve_law_jurisdictions,
)
from review_engine.law.staging import (
    LawStagingSink,
    LawStagingStore,
    StagingApprovalError,
)
from review_engine.law.web import (
    Citation,
    FeatureDisabled,
    JurisdictionLeak,
    LawQuery,
    NoPIIViolation,
    ProxyHttpTransport,
    SOURCE_SYSTEMS,
    WebLawIngestPipeline,
)
from review_engine.law.web.adapters import EgressBlocked, MissingCredential
from review_engine.law.web.pipeline import EmptyStatutoryText
from review_engine.config.settings import LAW_WEB_INGEST_ENABLED
from review_engine.llm_connectors.ollama import OllamaConnector
from review_engine.privacy.erasure import erase_matter
from review_engine.reports.generator import generate_docx_report, generate_pdf_report
from review_engine.dashboard.view import render_dashboard
from review_engine.reports.decisions import default_decisions_path, load_decisions
from review_engine.reviewer import decisions as reviewer_decisions

# RAYAAAA-269: the assistant/brand mark is a generated emoji-free robot SVG
# (robot standing in front of the RAYSERR badge). Loaded once and reused as the
# browser tab icon and, base64-inlined, as the floating-assistant FAB face.
_ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets")
_ROBOT_SVG_PATH = os.path.join(_ASSET_DIR, "robot-assistant.svg")

# The app is single-owner / owner-internal (RAYAAAA-191); every law-staging
# approve/reject decision is attributed to the owner in the audit log.
OWNER_NAME = "owner"


def _robot_svg_data_uri() -> str:
    """Base64 data URI of the robot mark for use as a CSS background-image."""
    with open(_ROBOT_SVG_PATH, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


st.set_page_config(
    page_title="RAYSERR Lens · AI Document Review",
    page_icon=_ROBOT_SVG_PATH,
    layout="wide",
)

# RAYAAAA-260: reskin to match the live marketing site (rayserrsolutions.com) and
# add a teal accent (owner ask, RAYAAAA-191). RAYAAAA-263: extend the same tokens
# to the base44 "Aich-R" shell — a restructured navy sidebar (brand lockup / MENU
# + ADMIN nav / user footer), the dashboard stat-tiles + review-type cards, and
# the bottom-right floating assistant. The core palette (navy sidebar / #f7f8fc
# content / teal primary) is set in .streamlit/config.toml; this block pins the
# look the theme keys can't reach.
st.markdown(
    """
    <style>
      /* --- RAYSERR brand tokens (marketing.css) + teal accent (RAYAAAA-260) --- */
      :root {
        --rs-navy: #1b2f5b;
        --rs-teal: #2a9d8f;
        --rs-teal-hover: #238577;
        --rs-border: #e4e9f0;
        --rs-shadow: 0 1px 6px rgba(27, 47, 91, 0.07);
        --rs-shadow-md: 0 4px 20px rgba(27, 47, 91, 0.11);
      }

      /* Hide leftover Streamlit chrome so only the RAYSERR brand shows. */
      footer {visibility: hidden;}
      div[data-testid="stDecoration"] {display: none;}
      a[href^="https://streamlit.io"], a[href^="https://share.streamlit.io"],
      div[data-testid="stStatusWidget"] {display: none !important;}

      /* Headings: Georgia serif to mirror rayserrsolutions.com headings. */
      [data-testid="stHeading"] h1,
      [data-testid="stHeading"] h2,
      [data-testid="stHeading"] h3,
      .stMarkdown h1, .stMarkdown h2, .stMarkdown h3 {
        font-family: Georgia, "Times New Roman", serif !important;
        font-weight: 400 !important;
        color: var(--rs-navy);
        letter-spacing: 0.1px;
      }

      /* Primary buttons + the teal accent on hover for all buttons. */
      .stButton > button:hover,
      .stDownloadButton > button:hover,
      .stFormSubmitButton > button:hover {
        border-color: var(--rs-teal) !important;
        color: var(--rs-teal) !important;
      }
      button[kind="primary"], button[data-testid="baseButton-primary"] {
        background: var(--rs-teal) !important;
        border-color: var(--rs-teal) !important;
      }
      button[kind="primary"]:hover,
      button[data-testid="baseButton-primary"]:hover {
        background: var(--rs-teal-hover) !important;
        border-color: var(--rs-teal-hover) !important;
      }

      /* Bordered containers / expanders read as the site's soft cards. */
      div[data-testid="stExpander"] > details,
      div[data-testid="stVerticalBlockBorderWrapper"] {
        border-color: var(--rs-border) !important;
        box-shadow: var(--rs-shadow);
      }

      /* --- RAYAAAA-263/269: RAYSERR Lens shell -------------------------------- */
      section[data-testid="stSidebar"] > div { padding-top: 1rem; }

      /* Sidebar brand lockup (R monogram + RAYSERR Lens / AI DOCUMENT REVIEW). */
      .aichr-brand { display:flex; align-items:center; gap:0.6rem; padding:0.25rem 0.35rem 0.6rem; }
      .aichr-brand .shield { width:36px;height:36px;border-radius:9px;
        background:linear-gradient(135deg,#2a9d8f,#238577);display:flex;align-items:center;
        justify-content:center;font-size:1.15rem;box-shadow:var(--rs-shadow);
        color:#fff;font-weight:700;font-family:Georgia,serif; }
      .aichr-brand .brand-text { display:flex;flex-direction:column;line-height:1.12; }
      .aichr-brand .brand-name { color:#fff;font-weight:700;font-size:1.08rem;font-family:Georgia,serif; }
      .aichr-brand .brand-tag { color:#9fb3d4;font-size:0.62rem;letter-spacing:1px;font-weight:600; }

      /* MENU / ADMIN section labels. */
      .nav-section { color:#7f97bd;font-size:0.66rem;letter-spacing:1.2px;font-weight:700;
        margin:0.9rem 0 0.15rem 0.45rem; }

      /* Sidebar nav buttons rendered as left-aligned nav items; active = teal. */
      section[data-testid="stSidebar"] .stButton > button {
        justify-content:flex-start; text-align:left; border:none; background:transparent;
        color:#e3ebf7; font-weight:500; box-shadow:none; padding:0.4rem 0.65rem;
      }
      section[data-testid="stSidebar"] .stButton > button:hover {
        background:rgba(255,255,255,0.07); color:#fff !important; border:none !important;
      }
      section[data-testid="stSidebar"] .stButton > button[kind="primary"] {
        background:var(--rs-teal) !important; color:#fff !important; font-weight:600;
        box-shadow:var(--rs-shadow);
      }

      /* Dashboard headers + panels. */
      .dash-welcome { font-size:2rem; margin:0 0 0.1rem; }
      .dash-subtitle { color:#6b7488; margin:0 0 0.4rem; }
      .panel-title { margin:0 0 0.35rem; font-size:1.05rem; }

      /* Stat tiles. */
      .stat-chip { width:42px;height:42px;border-radius:11px;display:flex;align-items:center;
        justify-content:center;font-size:1.2rem;margin-bottom:0.55rem; }
      .stat-value { font-size:1.9rem;font-weight:700;color:var(--rs-navy);line-height:1; }
      .stat-label { color:#6b7488;font-size:0.85rem;margin-top:0.15rem; }

      /* Review-type cards. */
      .review-card { display:flex;align-items:center;gap:0.6rem; }
      .review-chip { width:38px;height:38px;border-radius:10px;display:flex;align-items:center;
        justify-content:center;font-size:1.1rem;flex:none; }
      .review-text { display:flex;flex-direction:column;line-height:1.2; }
      .review-title { font-weight:600;color:var(--rs-navy);font-size:0.92rem; }
      .review-sub { color:#8a93a6;font-size:0.78rem; }

      /* Recent requests list. */
      .recent-row { display:flex;justify-content:space-between;padding:0.5rem 0;
        border-bottom:1px solid var(--rs-border); }
      .recent-name { font-weight:600;color:var(--rs-navy); }
      .recent-meta { color:#8a93a6;font-size:0.82rem; }
      .empty-note { text-align:center;color:#8a93a6;padding:2rem 0; }

      /* Floating assistant: fixed bottom-right FAB + teal-gradient panel.
         RAYAAAA-269: bigger FAB whose face is the generated robot mark
         (background-image set below, base64-inlined from assets/robot-assistant.svg).
         The text label ("Assistant") is kept for screen readers but hidden. */
      div[data-testid="stPopover"] { position:fixed; bottom:1.5rem; right:1.5rem;
        z-index:1000; width:auto; }
      div[data-testid="stPopover"] > button {
        border-radius:50% !important; width:74px; height:74px; padding:0;
        font-size:0 !important; color:transparent !important;
        background:linear-gradient(135deg,#2a9d8f,#238577) !important;
        background-size:cover !important; background-position:center !important;
        border:3px solid #fff !important; box-shadow:var(--rs-shadow-md);
        transition:transform .12s ease; }
      div[data-testid="stPopover"] > button:hover { transform:scale(1.06); }
      div[data-testid="stPopoverBody"] { min-width:340px; max-width:380px; }
      .aichr-assistant-header { display:flex;align-items:center;gap:0.6rem;
        background:linear-gradient(135deg,#2a9d8f,#1f7a6d); margin:-1rem -1rem 0.8rem;
        padding:0.9rem 1rem; border-radius:8px 8px 0 0; }
      .aichr-assistant-badge { width:34px;height:34px;border-radius:9px;
        background:rgba(255,255,255,0.18);display:flex;align-items:center;justify-content:center;
        color:#fff; }
      .aichr-assistant-titles { display:flex;flex-direction:column;line-height:1.15; }
      .aichr-assistant-name { color:#fff;font-weight:700; }
      .aichr-assistant-sub { color:#d6f2ee;font-size:0.72rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

# RAYAAAA-269: paint the generated robot mark onto the floating-assistant FAB.
# Kept separate from the static block above so the base64 data URI stays isolated.
st.markdown(
    "<style>div[data-testid=\"stPopover\"] > button {"
    f"background-image:url('{_robot_svg_data_uri()}') !important;}}</style>",
    unsafe_allow_html=True,
)


@st.cache_resource
def service() -> ReviewService:
    return ReviewService()


svc = service()

# Session-state single-page router. Every sidebar nav item + dashboard shortcut
# sets ``nav``; the dispatch at the bottom renders the matching view.
if "nav" not in st.session_state:
    st.session_state["nav"] = "dashboard"

# Single MENU nav, mirroring the owner's demo. RAYAAAA-269: the separate "ADMIN"
# group and the bottom user/identity footer were removed per owner feedback; the
# Review Queue (cross-Task risk dashboard) and the per-jurisdiction Law Library
# the owner still wants are folded into this one MENU so they stay reachable.
# "MCP Connections" stays OMITTED (cancelled, RAYAAAA-242). Nav items are clean
# text (no emoji) per the RAYSERR Lens rebrand.
_NAV_MENU = [
    ("dashboard", "Dashboard"),
    ("new_request", "New Request"),
    ("my_requests", "My Requests"),
    ("policy_library", "Policy Library"),
    ("review_queue", "Review Queue"),
    ("law_library", "Law Library"),
]


def _nav_button(key: str, label: str) -> None:
    active = st.session_state["nav"] == key
    if st.button(
        label,
        key=f"nav_{key}",
        use_container_width=True,
        type="primary" if active else "secondary",
    ):
        st.session_state["nav"] = key
        st.rerun()


with st.sidebar:
    # Brand lockup: RAYSERR Lens "R" monogram + "AI DOCUMENT REVIEW" (RAYAAAA-269
    # rebrand; the shield emoji is replaced by a clean teal "R" tile).
    st.markdown(
        "<div class='aichr-brand'><div class='shield'>R</div>"
        "<div class='brand-text'><span class='brand-name'>RAYSERR Lens</span>"
        "<span class='brand-tag'>AI DOCUMENT REVIEW</span></div></div>",
        unsafe_allow_html=True,
    )
    # RAYAAAA-227: persistent link back to the admin console so the Review Engine
    # is never a dead end (plain outbound link; the auth proxy / PII gate are
    # untouched).
    st.markdown(
        "<a href='https://rayserrsolutions.com/admin' target='_top' "
        "style='display:inline-block;margin:0 0 0.4rem 0.35rem;color:#4ac0b0;"
        "font-weight:600;text-decoration:none;font-size:0.82rem;'>"
        "← Back to RAYSERR Admin</a>",
        unsafe_allow_html=True,
    )

    st.markdown("<div class='nav-section'>MENU</div>", unsafe_allow_html=True)
    for _key, _label in _NAV_MENU:
        _nav_button(_key, _label)

    # RAYAAAA-269: the bottom user/identity footer (avatar + name + role + logout)
    # was removed per owner feedback ("remove admin section from the bottom parts,
    # all"). The "Back to RAYSERR Admin" link above returns to the admin console,
    # so it also serves as the logout affordance (auth is enforced by the nginx
    # proxy; no in-app auth change).

# RAYAAAA-259/263: the always-here floating assistant, rendered ONCE here (outside
# every view branch) so it is present on every view, backed by the LOCAL model
# (RAYAAAA-258). main.py CSS fixes it to the bottom-right as a teal FAB. Flag
# gating / retrieval / isolation / disclaimer are unchanged.
from review_engine.app.floating_assistant_view import render_floating_assistant

render_floating_assistant(svc)

notice = st.session_state.pop("_deleted_notice", None)
if notice:
    st.success(notice)

# Shared client lookups (used across the re-homed views).
clients = svc.db.list_clients()
client_label = {
    c["id"]: f"{c['display_name']} · {state_label(c['state'])}" for c in clients
}


# ---------------------------------------------------------------------------
# Re-homed views. Each is a former sidebar-radio view, now dispatched by nav.
# ---------------------------------------------------------------------------
def _render_new_request(svc) -> None:
    # RAYAAAA-263 shell + RAYAAAA-264 wizard. The base44 shell has no sidebar
    # "Clients" section, so client creation is re-homed here (the 264 wizard only
    # SELECTS a client). A "Create a client" expander runs above the 264 two-step
    # review-type wizard (which reads the dashboard card's ``nr_type`` prefilter).
    with st.expander("Create a client", expanded=not clients):
        with st.form("nr_create_client"):
            client_name = st.text_input("Client name")
            client_state = st.selectbox(
                "Jurisdiction (US state)",
                options=JURISDICTION_CHOICES,
                index=JURISDICTION_CHOICES.index(UNSPECIFIED_STATE),
                format_func=state_label,
            )
            if st.form_submit_button("Create client", type="primary"):
                if client_name.strip():
                    svc.db.create_client(client_name, client_state)
                    st.rerun()
                else:
                    st.error("Client name is required.")
    # RAYAAAA-264: the base44 two-step New Request wizard (presets over the
    # existing pipeline). Handles task creation + submission itself.
    render_new_request(svc, clients, client_label)


def _render_law_web_search(store: LawStagingStore) -> None:
    # RAYAAAA-287 (RAYAAAA-270 P2↔P3): the owner's "Search official government
    # sources" action. The form collects ONLY structured citation locators — there
    # is deliberately no free-text field, so no PII can reach the wire; LawQuery /
    # Citation validate every token against a citation charset (Counsel Cond C).
    # A hit is fetched via the RAYAAAA-274 pipeline and STAGED into the same
    # Pending Review queue below (LawStagingSink) — auto-add is FORBIDDEN.
    with st.expander("Search official government sources", expanded=False):
        st.caption(
            "Fetch statute / regulation text from an official U.S. government "
            "publisher (GovInfo, Congress.gov, eCFR) by its citation. Only the "
            "structured citation below is sent — never free text. Anything found "
            "lands in Pending Review for your Approve/Reject; nothing is added "
            "automatically. Public-domain statutory text only."
        )
        with st.form("law_web_search"):
            row1 = st.columns(2)
            with row1[0]:
                jurisdiction = st.selectbox(
                    "Jurisdiction",
                    options=LAW_JURISDICTION_CHOICES,
                    index=0,
                    format_func=law_jurisdiction_label,
                    key="law_web_jurisdiction",
                )
            with row1[1]:
                source_system = st.selectbox(
                    "Official source",
                    options=list(SOURCE_SYSTEMS),
                    format_func=lambda s: {
                        "govinfo": "GovInfo (GPO) — U.S. Code / CFR / Public Laws",
                        "congress": "Congress.gov (LoC) — bills / public laws",
                        "ecfr": "eCFR — Code of Federal Regulations",
                    }.get(s, s),
                    key="law_web_source_system",
                )
            st.markdown("**Citation locator** (structured tokens only — no free text)")
            row2 = st.columns(3)
            with row2[0]:
                c_title = st.text_input("Title", key="law_web_title", placeholder="29")
                c_collection = st.text_input(
                    "Collection", key="law_web_collection", placeholder="USCODE / CFR / PLAW"
                )
            with row2[1]:
                c_part = st.text_input("Part", key="law_web_part", placeholder="1630")
                c_congress = st.text_input("Congress", key="law_web_congress", placeholder="118")
            with row2[2]:
                c_section = st.text_input("Section", key="law_web_section", placeholder="552")
                c_version = st.text_input(
                    "Version date", key="law_web_version", placeholder="2023-01-01"
                )
            c_identifier = st.text_input(
                "Identifier (package / bill id)", key="law_web_identifier", placeholder="hr-1"
            )
            submitted = st.form_submit_button(
                "Search official sources", type="primary"
            )
        if submitted:
            query = LawQuery(
                jurisdiction=jurisdiction,
                source_system=source_system,
                citation=Citation(
                    title=c_title,
                    part=c_part,
                    section=c_section,
                    collection=c_collection,
                    identifier=c_identifier,
                    congress=c_congress,
                    version_date=c_version,
                ),
            )
            # Route staging through the SAME store the queue reads (RAYAAAA-287).
            pipeline = WebLawIngestPipeline(
                ProxyHttpTransport(), staging_sink=LawStagingSink(store)
            )
            try:
                with st.spinner("Fetching from the official source…"):
                    result = pipeline.ingest(query)
                st.success(
                    f"Fetched **{result.document_name}** "
                    f"({result.chunk_count} chunk(s)) into Pending Review. "
                    "Approve it below to add it to the live law library."
                )
                st.rerun()
            except NoPIIViolation as exc:
                st.error(f"Query rejected (not a valid citation locator): {exc}")
            except MissingCredential as exc:
                st.warning(
                    "This source needs an api.data.gov API key that isn't configured "
                    "yet — it's inert until an admin provisions it. eCFR needs no key. "
                    f"({exc})"
                )
            except EgressBlocked as exc:
                st.error(f"Blocked outbound request: {exc}")
            except JurisdictionLeak as exc:
                st.error(f"Jurisdiction mismatch — not staged: {exc}")
            except EmptyStatutoryText as exc:
                st.warning(f"Nothing statutory found to stage: {exc}")
            except FeatureDisabled as exc:
                st.error(str(exc))
            except Exception as exc:  # network / parse errors surface, not crash
                st.error(f"Could not fetch from the official source: {exc}")


def _render_law_pending_review(svc) -> None:
    # RAYAAAA-275 (RAYAAAA-270 P3): the "Pending Review" staging queue. Web-fetched
    # laws (RAYAAAA-274 P2) land here and go NOWHERE near the live index until the
    # owner clicks Approve — auto-add is FORBIDDEN (RAYAAAA-243 / Counsel + CTO-5).
    # The whole section is gated behind LAW_WEB_INGEST_ENABLED (OFF by default), so
    # the surface is INERT until the RAYAAAA-270 cutover clears its gates.
    if not LAW_WEB_INGEST_ENABLED:
        return
    store = LawStagingStore()
    # RAYAAAA-287: the owner-facing "search official gov sources" trigger. It runs
    # the RAYAAAA-274 pipeline through the SAME store this queue reads (LawStagingSink),
    # so a fetched law appears in the Pending Review list below — never auto-added.
    _render_law_web_search(store)
    pending = store.list_pending()
    st.markdown("### Pending review — web-fetched laws")
    st.caption(
        "Laws the AI search fetched from official government publishers, awaiting "
        "your decision. Nothing here is searchable or citable yet. Approve adds a "
        "record to the live law library (with its provenance); Reject discards it. "
        "Every decision is logged. Auto-add is disabled by policy."
    )
    if not pending:
        st.info("No web-fetched laws are waiting for review.")
    for item in pending:
        badges = []
        badges.append("✅ Official source" if item.official_source else "⚠️ Source not verified official")
        badges.append("📜 Statutory text only" if item.statutory_only else "⚠️ Annotated — not pure statute")
        with st.container(border=True):
            st.markdown(f"**{item.jurisdiction_label}** &nbsp; · &nbsp; " + " &nbsp; ".join(badges))
            meta_cols = st.columns(2)
            with meta_cols[0]:
                st.markdown(f"**Source:** {item.source_name or '—'}")
                st.markdown(f"**Source URL:** [{item.source_url}]({item.source_url})")
            with meta_cols[1]:
                st.markdown(f"**Retrieved:** {item.retrieved or '—'}")
                st.markdown(f"**Effective / version:** {item.effective or '—'}")
            if item.provenance_extra:
                st.caption(
                    "Provenance: "
                    + ", ".join(f"{k}={v}" for k, v in item.provenance_extra.items())
                )
            with st.expander("Extracted-text preview"):
                st.text(item.text_preview or "(no text extracted)")
            can_approve = item.official_source and item.statutory_only
            act_cols = st.columns([1, 1, 3])
            with act_cols[0]:
                if st.button(
                    "Approve → add to library",
                    key=f"law_stage_approve_{item.id}",
                    type="primary",
                    disabled=not can_approve,
                    help=None if can_approve else "Blocked: must be an official source AND pure statutory text (Counsel Cond A/B).",
                ):
                    try:
                        result = store.approve(item.id, svc, decided_by=OWNER_NAME)
                        st.success(
                            f"Approved into {law_jurisdiction_label(result['jurisdiction'])} "
                            f"law library ({result.get('chunks', 0)} chunks indexed)."
                        )
                        st.rerun()
                    except (StagingApprovalError, ValueError) as exc:
                        st.error(str(exc))
            with act_cols[1]:
                if st.button("Reject", key=f"law_stage_reject_{item.id}"):
                    store.reject(item.id, decided_by=OWNER_NAME, reason="owner rejected in review queue")
                    st.info("Discarded. It was not added to the law library.")
                    st.rerun()
    audit = store.audit_entries(limit=10)
    if audit:
        with st.expander("Recent review decisions (audit trail)"):
            st.dataframe(
                [
                    {
                        "When": e.get("at"),
                        "Decision": e.get("action"),
                        "By": e.get("decided_by"),
                        "Jurisdiction": law_jurisdiction_label(e.get("jurisdiction", "")),
                        "Source": e.get("source_name"),
                        "Source URL": e.get("source_url"),
                    }
                    for e in audit
                ],
                use_container_width=True,
                hide_index=True,
            )
    st.divider()


def _render_law_library(svc) -> None:
    # RAYAAAA-251 (Phase C): per-JURISDICTION law corpus (statute/regulation text
    # from OFFICIAL government publishers, per the RAYAAAA-243 Counsel memo).
    # RAYAAAA-275: the web-ingest "Pending Review" queue renders above the manual
    # upload surface (INERT unless LAW_WEB_INGEST_ENABLED).
    _render_law_pending_review(svc)
    st.subheader("Law reference library")
    st.caption(
        "Upload statute/regulation text from OFFICIAL government publishers "
        "(federal: eCFR / GovInfo / Cornell LII; state: the state's official "
        "code), keyed by jurisdiction. Public-domain law, shared across all "
        "clients in that jurisdiction and kept separate from client data. "
        "Do NOT paste from a paid legal database. Synthetic / owner-internal only."
    )
    law_jurisdiction = st.selectbox(
        "Jurisdiction",
        options=LAW_JURISDICTION_CHOICES,
        index=0,
        format_func=law_jurisdiction_label,
        key="law_lib_jurisdiction",
    )
    st.markdown("**Provenance (all fields required)**")
    prov_cols = st.columns(2)
    with prov_cols[0]:
        law_source_name = st.text_input(
            "Source / official publisher", key="law_source_name",
            placeholder="e.g. Cornell LII, eCFR, California Legislative Information",
        )
        law_effective = st.text_input(
            "Effective date / version", key="law_effective",
            placeholder="e.g. 2024 ed., or effective 2024-01-01",
        )
    with prov_cols[1]:
        law_source_url = st.text_input(
            "Source URL", key="law_source_url",
            placeholder="https://www.ecfr.gov/…",
        )
        law_retrieved = st.text_input(
            "Retrieval date (YYYY-MM-DD)", key="law_retrieved",
            placeholder="2026-07-13",
        )
    law_uploads = st.file_uploader(
        "Upload law documents",
        type=["pdf", "docx", "txt", "csv", "xlsx", "png", "jpg", "jpeg", "zip"],
        accept_multiple_files=True,
        key="law_uploader",
        help="Stored under this jurisdiction's local law library; not sent for model training.",
    )
    _provenance_ready = all(
        (law_source_name.strip(), law_source_url.strip(), law_effective.strip(), law_retrieved.strip())
    )
    if law_uploads and not _provenance_ready:
        st.warning("All four provenance fields are required before saving law documents.")
    if st.button(
        "Save law files",
        disabled=not (law_uploads and _provenance_ready),
        key="law_save",
    ):
        saved = 0
        for uploaded in law_uploads:
            try:
                svc.save_law_upload(
                    law_jurisdiction, uploaded.name, uploaded.getvalue(),
                    source_name=law_source_name, source_url=law_source_url,
                    effective=law_effective, retrieved=law_retrieved,
                )
                saved += 1
            except ValueError as exc:
                st.error(f"{uploaded.name}: {exc}")
        if saved:
            st.success(f"Saved {saved} law document(s) for {law_jurisdiction_label(law_jurisdiction)}.")
            st.rerun()
    law_docs = svc.db.list_law_documents(law_jurisdiction)
    if law_docs:
        st.dataframe(
            [
                {
                    "Law document": item["name"],
                    "Source": item["source_name"],
                    "Effective": item["effective"],
                    "Retrieved": item["retrieved"],
                    "Indexed": item["processed_at"] or "No",
                }
                for item in law_docs
            ],
            use_container_width=True,
            hide_index=True,
        )
        col_proc, col_del = st.columns(2)
        with col_proc:
            if st.button("Process law library", type="primary", key="law_process"):
                with st.spinner("Extracting and indexing this jurisdiction's law…"):
                    result = svc.process_law_library(law_jurisdiction)
                if result["errors"]:
                    st.warning("\n".join(result["errors"]))
                st.success(
                    f"Indexed {result['processed']} law document(s) into "
                    f"{result['chunks']} source chunks."
                )
        with col_del:
            to_delete = st.selectbox(
                "Remove a law document",
                options=["—"] + [d["name"] for d in law_docs],
                key="law_delete_pick",
            )
            if st.button("Delete selected law doc", disabled=to_delete == "—", key="law_delete"):
                svc.delete_law_document(law_jurisdiction, to_delete)
                st.success(f"Removed {to_delete} from the {law_jurisdiction_label(law_jurisdiction)} law library.")
                st.rerun()
    else:
        st.info("No law documents uploaded for this jurisdiction yet.")


def _render_my_requests(svc) -> None:
    # RAYAAAA-263: "My Requests" re-homes the former Task workspace. It lists the
    # requests (Tasks) and opens the selected one's full 12-tab workspace, so
    # every existing per-Task feature stays reachable.
    st.markdown(
        "<h1 class='dash-welcome'>My Requests</h1>"
        "<p class='dash-subtitle'>Your document reviews</p>",
        unsafe_allow_html=True,
    )
    matters = svc.db.list_matters()
    if not matters:
        st.info("No requests yet — create your first one.")
        if st.button("＋  New Request", type="primary", key="mr_new_request"):
            st.session_state["nav"] = "new_request"
            st.rerun()
        return
    name_by_id = {m["id"]: m["name"] for m in matters}
    ids = [m["id"] for m in matters]
    active = st.session_state.get("active_matter_id")
    if active not in ids:
        active = ids[0]
    picked = st.selectbox(
        "Select a request",
        options=ids,
        index=ids.index(active),
        format_func=lambda mid: name_by_id.get(mid, mid),
        key="my_requests_pick",
    )
    st.session_state["active_matter_id"] = picked
    matter = svc.db.get_matter(picked)
    _render_task_workspace(svc, matter, picked)


def _render_task_workspace(svc, matter, matter_id) -> None:
    header_col, delete_col = st.columns([5, 1])
    with header_col:
        st.subheader(f"{matter['name']} · {matter_id}")
        # RAYAAAA-244: a Task always resolves to exactly one Client, and its
        # jurisdiction is derived from that Client (never diverges).
        st.caption(
            f"Client: {matter.get('client_name') or '—'} · "
            f"Jurisdiction: {state_label(matter.get('jurisdiction') or UNSPECIFIED_STATE)}"
        )
    with delete_col:
        # RAYAAAA-228: owner-initiated in-process deletion of a task. This calls
        # erase_matter directly (NOT the HTTP fan-out endpoint). Streamlit has no
        # native confirm dialog, so require an explicit checkbox first.
        confirm = st.checkbox("Confirm delete", key=f"confirm_delete_{matter_id}")
        if st.button("Delete task", type="primary", disabled=not confirm):
            report = erase_matter(matter_id, svc.db.path)
            svc.db.log("matter_deleted", None, f"{matter['name']} ({matter_id})")
            if report.clean:
                st.session_state["_deleted_notice"] = f"Deleted task {matter['name']}."
            else:
                st.session_state["_deleted_notice"] = (
                    f"Deleted task {matter['name']} with residual: {report.residual_summary()}"
                )
            st.session_state.pop("active_matter_id", None)
            st.rerun()
    if (matter.get("jurisdiction") or UNSPECIFIED_STATE) == UNSPECIFIED_STATE:
        st.warning(
            "Jurisdiction unspecified — set the client's US state for "
            "jurisdiction-dependent legal review."
        )

    tabs = st.tabs(
        [
            "Documents",
            "Search evidence",
            "Run review",
            "Timeline",
            "Findings",
            "Export report",
            "Audit log",
            "Chat",
            "Policy audit",
            "Law grounding",
            "Compare",
            "Review",
        ]
    )

    with tabs[0]:
        uploads = st.file_uploader(
            "Upload original documents",
            type=["pdf", "docx", "txt", "csv", "xlsx", "png", "jpg", "jpeg", "zip"],
            accept_multiple_files=True,
            help="Files stay under this local task workspace and are not sent for model training.",
        )
        if st.button("Save uploaded files", disabled=not uploads):
            for uploaded in uploads:
                svc.save_upload(matter_id, uploaded.name, uploaded.getvalue())
            st.success(f"Saved {len(uploads)} file(s).")
            st.rerun()
        documents = svc.db.list_documents(matter_id)
        if documents:
            st.dataframe(
                [
                    {
                        "Document": item["name"],
                        "Type": item["file_type"],
                        "Bytes": item["size"],
                        "Processed": item["processed_at"] or "No",
                    }
                    for item in documents
                ],
                use_container_width=True,
                hide_index=True,
            )
            if st.button("Process documents", type="primary"):
                with st.spinner("Extracting, identifying entities, and building the local index…"):
                    result = svc.process_matter(matter_id)
                if result["errors"]:
                    st.warning("\n".join(result["errors"]))
                st.success(f"Processed {result['processed']} document(s) into {result['chunks']} source chunks.")
        else:
            st.info("No documents uploaded.")

    with tabs[1]:
        query = st.text_input("Question or keyword", placeholder="termination date, invoice 1042, witness statement…")
        limit = st.slider("Maximum results", 1, 20, 8)
        if st.button("Search", disabled=not query.strip()):
            try:
                results = svc.search(matter_id, query, limit)
                if not results:
                    st.info("No indexed evidence found. Process the matter first.")
                for result in results:
                    with st.expander(result["citation"]):
                        st.write(result["text"])
                        st.caption(f"Source: {result['source_ref']}")
            except Exception as exc:
                st.error(f"Search index unavailable: {exc}. Process the documents first.")

    with tabs[2]:
        include_hr = st.checkbox("HR / legal risk review", value=True)
        include_fraud = st.checkbox("Potential fraud indicator review", value=True)
        st.caption("Rules and anomaly scores identify review flags, not legal conclusions or proof of fraud.")
        if st.button("Run selected reviews", type="primary"):
            findings = svc.run_reviews(matter_id, include_hr, include_fraud)
            st.success(f"Review complete: {len(findings)} source-supported finding(s).")

    with tabs[3]:
        timeline = svc.timeline(matter_id)
        if timeline:
            st.dataframe(timeline, use_container_width=True, hide_index=True)
        else:
            st.info("No dated events identified in processed evidence.")

    with tabs[4]:
        findings = svc.db.get_findings(matter_id)
        if not findings:
            st.info("No source-supported findings. Process documents and run a review.")
        for finding in findings:
            with st.expander(f"{finding['category']} · {finding['title']} · {finding['confidence']}"):
                st.write(finding["explanation"])
                st.caption(f"Confidence basis: {finding['confidence_reason']}")
                st.write("Sources:")
                for source in finding["supporting_sources"]:
                    st.markdown(f"- {source['citation']}")

    with tabs[5]:
        findings = svc.db.get_findings(matter_id)
        summary = None
        ollama_enabled = st.checkbox("Use local Ollama to draft the executive summary", value=False)
        if ollama_enabled:
            model = st.text_input("Ollama model", value="llama3.2")
            connector = OllamaConnector(model=model)
            if st.button("Draft summary with Ollama"):
                if connector.available():
                    with st.spinner("Drafting only from existing findings…"):
                        st.session_state["ollama_summary"] = connector.summarize_findings(findings)
                else:
                    st.error("Ollama is not available at the local address.")
            summary = st.text_area(
                "Executive summary",
                value=st.session_state.get("ollama_summary", ""),
                height=180,
            ) or None
        decisions = load_decisions(default_decisions_path(svc.db.path, matter_id), matter_id)
        if decisions:
            st.caption(f"Including {len(decisions)} reviewer decision(s) in the report.")
        col1, col2 = st.columns(2)
        with col1:
            docx = generate_docx_report(svc.db, matter_id, executive_summary=summary, decisions=decisions)
            if st.download_button(
                "Download DOCX report", docx, f"{matter_id}_review_report.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ):
                svc.db.log("report_generated", matter_id, "DOCX")
        with col2:
            pdf = generate_pdf_report(svc.db, matter_id, executive_summary=summary, decisions=decisions)
            if st.download_button(
                "Download PDF report", pdf, f"{matter_id}_review_report.pdf", "application/pdf"
            ):
                svc.db.log("report_generated", matter_id, "PDF")

    with tabs[6]:
        logs = svc.db.get_audit_log(matter_id)
        st.dataframe(logs, use_container_width=True, hide_index=True)

    with tabs[7]:
        # RAYAAAA-232 (P2a): grounded RAG chat. Answers ONLY from this Task's
        # indexed evidence; local model only; degrades to raw passages offline.
        st.caption(
            "Ask a question about this Task's documents AND this Client's policy "
            "library. Answers are drawn only from the local evidence indexes "
            "(Task + linked-client policies) and cite source references. Requires "
            "human review."
        )
        question = st.text_input(
            "Your question", key="chat_question",
            placeholder="What are the termination terms? Is there a liability cap?",
        )
        if st.button("Ask", key="chat_ask", disabled=not question.strip()):
            with st.spinner("Retrieving evidence and drafting a grounded answer…"):
                answerer = GroundedAnswerer(retriever=make_client_scoped_retriever(svc.db))
                result = answerer.answer(matter_id, question)
            st.write(result["answer"])
            if result["sources"]:
                st.write("Sources:")
                for source in result["sources"]:
                    st.markdown(f"- {source['citation']}")
            if not result["model_used"]:
                st.info("Local model unavailable — showed retrieved passages only.")

    with tabs[8]:
        # RAYAAAA-233 (P2b): policy-audit / before-you-sign.
        st.caption(
            "\"Before you sign\": screens this Task's documents against this Client's "
            "own policy library plus a generic risky-clause checklist, flagging "
            "unusual/risky clauses and missing protections. Evidence-bound, "
            "local-only, and a screening aid — requires human review."
        )
        from review_engine.app.policy_audit import DEFAULT_CHECKLIST, checklist_from_policies

        audit_client_id = matter.get("client_id")
        policy_chunks = svc.db.get_policy_chunks(audit_client_id) if audit_client_id else []
        policy_checklist = checklist_from_policies(policy_chunks)
        audit_checklist = policy_checklist + DEFAULT_CHECKLIST
        if policy_checklist:
            st.caption(
                f"Auditing against {len(policy_checklist)} of this client's own "
                "policy document(s) plus the generic checklist."
            )
        else:
            st.caption(
                "This client has no processed policy library yet — running the "
                "generic checklist only. Add policies in the Policy Library view."
            )
        if st.button("Run before-you-sign review", type="primary", key="policy_audit_run"):
            with st.spinner("Screening retrieved clauses against the checklist…"):
                auditor = PolicyAuditor(retriever=make_client_scoped_retriever(svc.db))
                audit_findings = auditor.audit(matter_id, checklist=audit_checklist)
            st.session_state["policy_audit_findings"] = audit_findings
        audit_findings = st.session_state.get("policy_audit_findings")
        if audit_findings is None:
            st.info("Process the Task's documents, then run the review.")
        elif not audit_findings:
            st.success("No risky clauses or missing protections were flagged. Human review still required.")
        else:
            st.warning(f"{len(audit_findings)} item(s) to review before signing.")
            for finding in audit_findings:
                with st.expander(f"{finding['category']} · {finding['title']} · {finding['confidence']}"):
                    st.write(finding["explanation"])
                    st.caption(f"Confidence basis: {finding['confidence_reason']}")
                    if finding["supporting_sources"]:
                        st.write("Sources:")
                        for source in finding["supporting_sources"]:
                            st.markdown(f"- {source['citation']}")

    with tabs[9]:
        # RAYAAAA-251 (Phase C): law-grounded Q&A.
        law_juris = resolve_law_jurisdictions(matter.get("jurisdiction"))
        st.caption(
            "Ask a law-grounded question. Retrieval is restricted to this Task's "
            "documents, the linked client's policy library, and the law reference "
            f"corpus for {', '.join(law_jurisdiction_label(j) for j in law_juris)} "
            "only — never another state's. Evidence-bound; requires human review."
        )
        law_question = st.text_input(
            "Your law-grounded question", key="law_question",
            placeholder="What are the meal-break requirements? Which overtime rule applies?",
        )
        if st.button("Ask (law-grounded)", key="law_ask", disabled=not law_question.strip()):
            with st.spinner("Retrieving jurisdiction-scoped evidence and drafting a grounded answer…"):
                law_answerer = LawGroundedAnswerer(
                    retriever=make_law_grounded_retriever(svc.db)
                )
                law_result = law_answerer.answer(matter_id, law_question)
            st.info(law_result.disclaimer)
            st.write(law_result.answer)
            if law_result.redacted_citations:
                st.warning(
                    "Redacted citation(s) not backed by the reference library: "
                    + ", ".join(law_result.redacted_citations)
                )
            if law_result.law_sources:
                st.markdown("**Law sources (verbatim + provenance):**")
                for source in law_result.law_sources:
                    st.markdown(f"> {source['quote']}")
                    st.caption(f"{source['citation']} {source['stamp']}")
            if law_result.task_sources or law_result.policy_sources:
                st.markdown("**Other sources:**")
                for source in law_result.task_sources + law_result.policy_sources:
                    st.markdown(f"- {source['citation']}")
            if not law_result.model_used:
                st.info("Local model unavailable — showed retrieved passages only.")

    with tabs[10]:
        # RAYAAAA-231 (P1b): deterministic document compare / redline.
        st.caption(
            "Redline two processed documents in this matter: added, removed, and "
            "changed segments, each anchored to a source reference. Deterministic "
            "and local; requires human review."
        )
        processed_docs = [
            item["name"]
            for item in svc.db.list_documents(matter_id)
            if item["processed_at"]
        ]
        if len(processed_docs) < 2:
            st.info(
                "Upload and process at least two documents (or two versions) to "
                "compare them."
            )
        else:
            base_name = st.selectbox("Base version (earlier)", processed_docs, key="compare_base")
            compare_options = [name for name in processed_docs if name != base_name]
            compare_name = st.selectbox("Compared version (later)", compare_options, key="compare_target")
            show_unchanged = st.checkbox("Show unchanged segments", value=False, key="compare_unchanged")
            use_ollama = st.checkbox(
                "Draft a plain-language summary with local Ollama", value=False, key="compare_ollama"
            )
            if st.button("Compare documents", type="primary", key="compare_run"):
                with st.spinner("Diffing documents locally…"):
                    comparison = svc.compare_documents(
                        matter_id, base_name, compare_name, include_unchanged=show_unchanged
                    )
                counts = comparison.counts
                if not comparison.has_changes:
                    st.success("No differences found between the two versions.")
                else:
                    st.warning(
                        f"{counts['added']} added · {counts['removed']} removed · "
                        f"{counts['changed']} changed segment(s). Requires human review."
                    )
                if use_ollama:
                    from review_engine.compare.redline import summarize_comparison

                    connector = OllamaConnector()
                    with st.spinner("Drafting a grounded summary of the diff…"):
                        summary = summarize_comparison(comparison, connector)
                    st.markdown("**Summary of changes**")
                    st.write(summary)
                    if not connector.available():
                        st.info("Local model unavailable — showed the deterministic summary.")

                _badge = {
                    "added": (":green[ADDED]", "compare_source_refs", "compare_text"),
                    "removed": (":red[REMOVED]", "base_source_refs", "base_text"),
                    "changed": (":orange[CHANGED]", "compare_source_refs", None),
                    "unchanged": (":gray[UNCHANGED]", "compare_source_refs", "compare_text"),
                }
                for segment in comparison.segments:
                    label, ref_attr, _text_attr = _badge[segment.kind]
                    refs = getattr(segment, ref_attr) or segment.base_source_refs
                    header = f"{label} · {', '.join(refs) if refs else 'no source ref'}"
                    with st.expander(header):
                        if segment.kind == "changed":
                            st.markdown(f":red[- {segment.base_text}]")
                            st.markdown(f":green[+ {segment.compare_text}]")
                            st.caption(
                                "Base: "
                                + (", ".join(segment.base_citations) or "—")
                                + "  →  Compared: "
                                + (", ".join(segment.compare_citations) or "—")
                            )
                        elif segment.kind == "removed":
                            st.markdown(f":red[- {segment.base_text}]")
                            st.caption("Base: " + (", ".join(segment.base_citations) or "—"))
                        else:  # added / unchanged
                            st.markdown(f":green[+ {segment.compare_text}]")
                            st.caption("Compared: " + (", ".join(segment.compare_citations) or "—"))

    with tabs[11]:
        st.caption(
            "Human reviewer workspace — synthetic/local data only. Mark each source "
            "chunk approve / reject / needs-changes and add a note. Decisions are saved "
            "to this Task's workspace and are consumed by the branded report generator."
        )
        review_findings = svc.db.get_findings(matter_id)
        review_chunks = svc.db.get_chunks(matter_id)

        src_meta: dict[str, dict] = {}
        for finding in review_findings:
            for source in finding.get("supporting_sources", []):
                sid = source.get("source_ref")
                if not sid:
                    continue
                meta = src_meta.setdefault(
                    sid, {"citation": source.get("citation", sid), "findings": []}
                )
                meta["findings"].append(f"{finding['category']} · {finding['title']}")

        include_all = st.checkbox(
            "Include all indexed source chunks (not only those cited by findings)",
            value=not review_findings,
        )
        if include_all:
            for chunk in review_chunks:
                src_meta.setdefault(chunk.source_ref, {"citation": chunk.citation, "findings": []})

        src_ids = sorted(src_meta)
        store = reviewer_decisions.load_decisions(matter_id)
        counts = reviewer_decisions.summary_counts(store, src_ids)

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Approved", counts["approved"])
        c2.metric("Rejected", counts["rejected"])
        c3.metric("Needs changes", counts["needs_changes"])
        c4.metric("Undecided", counts["undecided"])
        c5.metric("Total chunks", counts["total"])

        if not src_ids:
            st.info(
                "No source chunks to review yet. Upload documents, process the Task, "
                "and run a review (or tick the box above once chunks are indexed)."
            )
        else:
            status_options = list(reviewer_decisions.VALID_STATUSES)
            status_labels = {
                "approved": "Approved",
                "rejected": "Rejected",
                "needs_changes": "Needs changes",
                "undecided": "Undecided",
            }
            reviewer = st.text_input("Reviewer", value="reviewer", key="review_reviewer")
            with st.form("reviewer_decisions_form"):
                pending: dict[str, dict] = {}
                for sid in src_ids:
                    meta = src_meta[sid]
                    current = reviewer_decisions.get_decision(store, sid)
                    st.markdown(f"**{sid}** — {meta['citation']}")
                    if meta["findings"]:
                        st.caption("Cited by: " + "; ".join(sorted(set(meta["findings"]))))
                    col_status, col_note = st.columns([1, 2])
                    status = col_status.selectbox(
                        "Decision",
                        status_options,
                        index=status_options.index(current["status"]),
                        format_func=lambda s: status_labels[s],
                        key=f"status_{sid}",
                    )
                    note = col_note.text_area(
                        "Note",
                        value=current["note"],
                        key=f"note_{sid}",
                        height=68,
                    )
                    pending[sid] = {"status": status, "note": note}
                    st.divider()
                if st.form_submit_button("Save decisions", type="primary"):
                    reviewer_decisions.save_decisions(matter_id, pending, reviewer=reviewer)
                    svc.db.log("reviewer_decisions_saved", matter_id, f"{len(pending)} chunk decisions")
                    st.success("Decisions saved to this Task's workspace.")
                    st.rerun()


# ---------------------------------------------------------------------------
# Router dispatch.
# ---------------------------------------------------------------------------
nav = st.session_state["nav"]
if nav == "dashboard":
    render_dashboard_home(svc)
elif nav == "new_request":
    _render_new_request(svc)
elif nav == "policy_library":
    # RAYAAAA-264 redesigned Policy Library (tabs / live counts / chips / search).
    render_policy_library(svc, clients, client_label)
elif nav == "law_library":
    _render_law_library(svc)
elif nav == "review_queue":
    render_dashboard(svc)
elif nav == "my_requests":
    _render_my_requests(svc)
else:
    render_dashboard_home(svc)
