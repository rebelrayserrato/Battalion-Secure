"""Always-here floating "Rai.8" chat widget (RAYAAAA-259, simplified RAYAAAA-294).

The owner (RAYAAAA-191) wanted a chat that is INDEPENDENT of any single Task — a
floating "Ask me anything" widget that sees across everything the owner owns (all
Tasks/clients/policies), unlike the per-Task Chat tab (RAYAAAA-232).

RAYAAAA-294 turns the earlier *form* (explainer paragraph + a Scope dropdown + a
labelled "Your question" field + an "Ask" button) into a plain **chat**: a
scrolling message thread of chat bubbles with a single message input pinned at the
bottom (Enter to send). The widget is renamed "Rai.8". The Scope dropdown is gone
— retrieval always uses the existing owner-scoped "all clients / all Tasks" path
(client_id=None), and the RAG layer already surfaces the relevant Task/client
from the question text and cites the Task + source it came from (AC4).

It is rendered from main.py on EVERY view (before any st.stop()), so it is
genuinely always available (AC1). Streamlit has no native fixed-position overlay
without a custom component; a st.popover pinned bottom-right (main.py CSS)
is the robust, theme-consistent "always here" surface — it inherits the
RAYAAAA-227/260 RAYSERR theme (navy + teal accent) automatically.

The brain is the LOCAL model (RAYAAAA-258, hermes3:3b) via
local_assistant.FloatingAssistant — NOT the cancelled external MCP multi-model
path. Owner-scoped cross-Task retrieval with HARD per-client isolation, grounding,
graceful degradation and provenance are inherited from RAYAAAA-247/232; this file
is only the thin rendering + input wiring.

Flag-gated (CROSS_TASK_ASSISTANT_ENABLED, OFF by default) and SYNTHETIC /
owner-internal only until the Phase 4 PII gate (RAYAAAA-196/198).
"""
from __future__ import annotations

import os

import streamlit as st

from review_engine.app.cross_task import CrossTaskAccessError, assistant_enabled
from review_engine.app.icons import icon as ui_icon
from review_engine.app.local_assistant import FloatingAssistant

# The closed popover trigger is the teal circular robot FAB at the bottom-right
# (main.py CSS makes it round, pins it bottom-right and paints the robot mark as
# its face while hiding this text label). The label is kept for screen readers
# and is the widget's name: "Rai.8" (RAYAAAA-294).
_WIDGET_LABEL = "Rai.8"
# Small monochrome robot glyph for the panel header badge (emoji-free).
_ROBOT_BADGE = ui_icon("robot", 20)
# Session key holding the chat transcript so it persists across popover reruns.
_HISTORY_KEY = "_rai8_history"


def render_floating_assistant(svc) -> None:
    """Render the always-here Rai.8 chat as a bottom-right floating panel.

    Rendered once from main.py OUTSIDE any view branch so it is present on
    every view — the "always here", independent-of-any-single-Task surface the
    owner asked for. The brain, gating, retrieval, isolation and disclaimer are
    unchanged (RAYAAAA-259/247/258); RAYAAAA-294 only simplifies the panel into a
    chat and renames it."""
    with st.popover(_WIDGET_LABEL, use_container_width=False):
        _render_panel(svc)


def _render_panel(svc) -> None:
    # Teal-gradient header: the robot badge + the "Rai.8 · Always here" name.
    st.markdown(
        "<div class='aichr-assistant-header'>"
        f"<span class='aichr-assistant-badge'>{_ROBOT_BADGE}</span>"
        "<span class='aichr-assistant-titles'>"
        "<span class='aichr-assistant-name'>Rai.8</span>"
        "<span class='aichr-assistant-sub'>Always here</span>"
        "</span></div>",
        unsafe_allow_html=True,
    )

    # Fail closed: the RAYAAAA-247 feature flag is OFF by default. When off, the
    # widget shows enablement guidance and never constructs the assistant.
    if not assistant_enabled():
        st.info(
            "Rai.8 is off. An operator enables it with "
            "`CROSS_TASK_ASSISTANT_ENABLED=1` (owner-internal, synthetic-only). "
            "The local model brain is served on-box (RAYAAAA-258) with no "
            "external egress; the Phase 4 PII gate still applies."
        )
        return

    # Scrolling message thread. Each turn is a chat bubble; user turns show the
    # question, Rai.8 turns re-render the grounded reply (answer + citations +
    # disclaimer) stored from when it was drafted.
    history = st.session_state.setdefault(_HISTORY_KEY, [])
    for turn in history:
        with st.chat_message(turn["role"]):
            if turn["role"] == "user":
                st.markdown(turn["text"])
            else:
                _render_reply(turn["reply"])

    # Single message input pinned at the bottom of the panel; Enter sends (AC3).
    prompt = st.chat_input("Ask me anything…", key="rai8_input")
    if prompt and prompt.strip():
        reply = _answer(svc, prompt.strip())
        if reply is not None:
            history.append({"role": "user", "text": prompt.strip()})
            history.append({"role": "assistant", "reply": reply})
            # Re-run so the new turns render in the thread above the input.
            st.rerun()


def _answer(svc, question: str):
    """Draft one grounded, owner-scoped reply for the given question.

    Auto-scope (AC4): no Client is selected in the UI, so retrieval always uses
    the owner-scoped "all clients / all Tasks" path (client_id=None). The RAG
    layer surfaces the relevant Task/client from the question text and cites them.
    Returns None (after surfacing an error) when access is denied."""
    token = os.getenv("CROSS_TASK_ASSISTANT_TOKEN")
    try:
        assistant = FloatingAssistant.create(svc.db, token=token, client_id=None)
    except CrossTaskAccessError as exc:
        st.error(f"Access denied: {exc}")
        return None
    # CPU-only local model ⇒ multi-second replies (RAYAAAA-258 benchmark). The
    # spinner shows progress; the connector times out rather than hanging, and any
    # model failure degrades to grounded excerpts (see _render_reply).
    with st.spinner("Searching across your Tasks and drafting a grounded answer…"):
        return assistant.ask(question)


def _render_reply(reply) -> None:
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

    # Source citations. Every cited SRC maps back to its Task + Client.
    with st.expander(f"Sources · {len(reply.sources)} across your Tasks", expanded=False):
        if not reply.sources:
            st.caption("No sources.")
        else:
            for source in reply.sources:
                st.markdown(f"- {source.label()}")

    # The standard "AI assists, human review required / not legal advice"
    # disclaimer, on every answer.
    st.caption(reply.disclaimer)
