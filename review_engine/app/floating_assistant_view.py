"""Always-here floating "personal assistant" widget (RAYAAAA-259).

The owner (RAYAAAA-191, demo "Aich-R Assistant · Always here") wanted a chat that
is INDEPENDENT of any single Task — a floating "Ask me anything" widget that sees
across everything the owner owns (all Tasks/clients/policies), unlike the per-Task
Chat tab (RAYAAAA-232). This renders exactly that.

It is rendered from the sidebar on EVERY view (``main.py`` calls it before any
``st.stop()``), so it is genuinely always available and never buried inside one
Task's tabs (AC1). Streamlit has no native fixed-position overlay without a custom
component; a sidebar ``st.popover`` pinned at the top is the robust,
theme-consistent "always here" surface — it inherits the RAYAAAA-227/260 RAYSERR
theme (navy + teal accent) automatically.

The brain is the LOCAL model (RAYAAAA-258, ``hermes3:3b``) via
``local_assistant.FloatingAssistant`` — NOT the cancelled external MCP multi-model
path. All the hard parts (owner-scoped cross-Task retrieval with HARD per-client
isolation, grounding, graceful degradation, provenance) are inherited from
RAYAAAA-247/232; this file is only the thin rendering + input wiring.

Flag-gated (``CROSS_TASK_ASSISTANT_ENABLED``, OFF by default) and SYNTHETIC /
owner-internal only until the Phase 4 PII gate (RAYAAAA-196/198).
"""
from __future__ import annotations

import os

import streamlit as st

from review_engine.app.cross_task import CrossTaskAccessError, assistant_enabled
from review_engine.app.local_assistant import FloatingAssistant
from review_engine.clients.jurisdictions import state_label

# The owner's demo shows a teal circular "chat" FAB at the bottom-right; the
# closed popover trigger is that FAB (just the icon — main.py CSS makes it round
# and pins it bottom-right). The panel's own teal-gradient header carries the
# "Aich-R Assistant · AI-powered · Always here" label from the demo.
_WIDGET_LABEL = "💬"
# Session key holding the last reply so it persists across the popover's reruns.
_REPLY_KEY = "_floating_assistant_reply"


def render_floating_assistant(svc) -> None:
    """Render the always-here assistant as a bottom-right floating panel.

    RAYAAAA-263: repositioned from the sidebar popover to the demo's bottom-right
    floating panel (``main.py`` CSS fixes the ``stPopover`` container to the
    bottom-right and rounds the trigger into a teal FAB). It is rendered once from
    ``main.py`` OUTSIDE any view branch so it is present on every view — the
    "always here", independent-of-any-single-Task surface the owner asked for. The
    brain, gating, retrieval, isolation and disclaimer are unchanged (RAYAAAA-259/
    247/258); only placement + skin changed."""
    # ``st.popover`` (Streamlit >=1.31; requirement pins >=1.35) gives a button
    # that opens a floating panel in place — the closest native equivalent to the
    # demo's "always here" widget.
    with st.popover(_WIDGET_LABEL, use_container_width=False):
        _render_panel(svc)


def _render_panel(svc) -> None:
    # Demo's teal-gradient header. Purely presentational; the label matches the
    # owner's "Aich-R Assistant · AI-powered · Always here" widget chrome.
    st.markdown(
        "<div class='aichr-assistant-header'>"
        "<span class='aichr-assistant-badge'>\U0001f4bc</span>"
        "<span class='aichr-assistant-titles'>"
        "<span class='aichr-assistant-name'>Aich-R Assistant</span>"
        "<span class='aichr-assistant-sub'>AI-powered · Always here</span>"
        "</span></div>",
        unsafe_allow_html=True,
    )
    st.markdown("**Ask me anything — across all your Tasks**")
    st.caption(
        "I see across every Task, client and policy you own (not one Task at a "
        "time). Answers are grounded only in your indexed evidence and cite the "
        "Task + source they came from. Synthetic / owner-internal data only."
    )

    # Fail closed: the RAYAAAA-247 feature flag is OFF by default. When off, the
    # widget shows enablement guidance and never constructs the assistant.
    if not assistant_enabled():
        st.info(
            "The assistant is off. An operator enables it with "
            "`CROSS_TASK_ASSISTANT_ENABLED=1` (owner-internal, synthetic-only). "
            "The local model brain is served on-box (RAYAAAA-258) with no "
            "external egress; the Phase 4 PII gate still applies."
        )
        return

    # Optional Client scope. Default spans every Task the owner owns; picking a
    # Client HARD-restricts retrieval to that Client's Tasks + policy library
    # (RAYAAAA-247 structural isolation — another client's index is never opened).
    clients = svc.db.list_clients()
    client_id = None
    if clients:
        label_of = {
            c["id"]: f"{c['display_name']} · {state_label(c['state'])}" for c in clients
        }
        options = ["__all__"] + [c["id"] for c in clients]
        picked = st.selectbox(
            "Scope",
            options=options,
            format_func=lambda cid: "All clients"
            if cid == "__all__"
            else label_of.get(cid, cid),
            key="floating_assistant_scope",
            help="Restrict me to one Client's Tasks, or span all of them.",
        )
        client_id = None if picked == "__all__" else picked

    question = st.text_input(
        "Your question",
        key="floating_assistant_question",
        placeholder="Ask me anything...",
    )

    if st.button(
        "Ask", key="floating_assistant_ask", type="primary", disabled=not question.strip()
    ):
        # The internal token (if configured) is read server-side only, never a UI
        # field. ``create`` enforces both auth gates and selects the LOCAL brain.
        token = os.getenv("CROSS_TASK_ASSISTANT_TOKEN")
        try:
            assistant = FloatingAssistant.create(svc.db, token=token, client_id=client_id)
        except CrossTaskAccessError as exc:
            st.error(f"Access denied: {exc}")
            return
        # CPU-only local model ⇒ multi-second replies (RAYAAAA-258 benchmark).
        # The spinner shows progress; the connector times out rather than hanging,
        # and any model failure degrades to grounded excerpts (below).
        with st.spinner("Searching across your Tasks and drafting a grounded answer…"):
            st.session_state[_REPLY_KEY] = assistant.ask(question)

    reply = st.session_state.get(_REPLY_KEY)
    if reply is not None:
        _render_reply(reply)


def _render_reply(reply) -> None:
    st.divider()
    if not reply.grounded:
        # Empty question or nothing retrieved: show the message, never a model
        # guess (mirrors RAYAAAA-232 — no evidence ⇒ no generation).
        st.warning(reply.answer)
        st.caption(reply.disclaimer)
        return

    st.markdown(reply.answer)

    # AC4: graceful degradation. When the local model is off/down/slow/errored the
    # reply carries the extractive excerpts plus this notice — a clear "model
    # unavailable" state, not a hang.
    if not reply.model_used and reply.notice:
        st.info(reply.notice)

    # AC2: source citations. Every cited SRC maps back to its Task + Client.
    with st.expander(f"Sources · {len(reply.sources)} across your Tasks", expanded=False):
        if not reply.sources:
            st.caption("No sources.")
        else:
            for source in reply.sources:
                st.markdown(f"- {source.label()}")

    # AC2: the standard "AI assists, human review required / not legal advice"
    # disclaimer, on every answer.
    st.caption(reply.disclaimer)
