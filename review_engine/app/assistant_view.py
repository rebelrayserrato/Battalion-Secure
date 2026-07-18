"""Streamlit surface for the multi-model cross-Task assistant (RAYAAAA-248, B3).

Renders the "Ask across all your Tasks" personal-assistant view. This is a
SEPARATE surface from the per-Task Chat tab (RAYAAAA-232) — it is reached from the
sidebar ``View`` selector in ``main.py`` and never touches a single Task's index.

The heavy lifting (retrieval + routing/fan-out) lives in the streamlit-free
``cross_task_chat`` module so it stays unit-testable; this file is only the thin
RAYSERR-branded (RAYAAAA-227) rendering + input wiring.
"""
from __future__ import annotations

import os

import streamlit as st

from review_engine.app.cross_task import CrossTaskAccessError, assistant_enabled
from review_engine.app.cross_task_chat import (
    AssistantResult,
    ModelAnswer,
    MODEL_LABELS,
    MultiModelAssistant,
    provider_for_label,
)

# The three owner-facing model labels, in the order the owner named them.
_MODEL_CHOICES = list(MODEL_LABELS.values())  # ["Codex", "Hermes", "Claude"]


def render_assistant(svc, clients=None, client_label=None) -> None:
    """Render the cross-Task assistant view.

    ``clients`` / ``client_label`` come from ``main.py``'s sidebar so the owner
    can optionally hard-scope the assistant to a single Client (reusing B2's
    structural cross-client isolation)."""
    st.subheader("Cross-Task assistant")
    st.caption(
        "Ask across ALL your Tasks at once. Route a question to one model or "
        "compare Codex, Hermes and Claude side by side — every answer is grounded "
        "only in your Tasks' documents and cites the Task + source it came from. "
        "Synthetic / owner-internal data only."
    )

    # Gate 1 (B2): the feature flag. OFF by default. Fail closed with guidance.
    if not assistant_enabled():
        st.info(
            "The cross-Task assistant is disabled. An operator enables it with "
            "`CROSS_TASK_ASSISTANT_ENABLED=1` (owner-internal, synthetic-only; "
            "the Phase 4 PII gate still applies)."
        )
        return

    # Optional Client scope. Default spans every Task the owner owns; picking a
    # Client hard-restricts retrieval to that Client's Tasks + policy library.
    client_id = None
    if clients:
        label_of = client_label or {}
        options = ["__all__"] + [c["id"] for c in clients]
        picked = st.selectbox(
            "Scope",
            options=options,
            format_func=lambda cid: "All clients"
            if cid == "__all__"
            else label_of.get(cid, cid),
            help="Restrict the assistant to one Client's Tasks, or span all of them.",
        )
        client_id = None if picked == "__all__" else picked

    # Per-query model selection: one model, or all (simultaneous compare).
    mode = st.radio(
        "Models",
        options=["Single model", "Compare all"],
        horizontal=True,
        help=(
            "Route the question to one model, or fan out to all of them at once "
            "and compare the answers side by side."
        ),
    )
    if mode == "Single model":
        chosen_label = st.selectbox("Model", options=_MODEL_CHOICES)
        providers = [provider_for_label(chosen_label)]
    else:
        providers = None  # None => every configured model

    question = st.text_input(
        "Question",
        key="assistant_question",
        placeholder="e.g. Which Tasks mention overtime disputes?",
    )

    if st.button("Ask", type="primary", disabled=not question.strip()):
        # Gate 2 (B2): optional internal token, read server-side only (never a UI
        # field). authorize() inside create() enforces both gates.
        token = os.getenv("CROSS_TASK_ASSISTANT_TOKEN")
        try:
            assistant = MultiModelAssistant.create(
                svc.db, token=token, client_id=client_id
            )
        except CrossTaskAccessError as exc:
            st.error(f"Access denied: {exc}")
            return
        with st.spinner("Retrieving across your Tasks and querying model(s)…"):
            result = assistant.ask(question, providers)
        _render_result(result)


def _render_result(result: AssistantResult) -> None:
    if not result.answers:
        # Empty question or no evidence retrieved: show the notice, nothing else.
        st.warning(result.notice or "No answer.")
        return

    if result.compared:
        st.markdown("**Side-by-side compare**")
        columns = st.columns(len(result.answers))
        for column, answer in zip(columns, result.answers):
            with column:
                _render_answer(answer)
    else:
        _render_answer(result.answers[0])

    _render_provenance(result)


def _render_answer(answer: ModelAnswer) -> None:
    st.markdown(f"**{answer.label}** · `{answer.model}`")
    if answer.mock:
        st.caption(
            "Mock response — no provider key configured (or egress disabled). "
            "Synthetic placeholder, not a real model answer."
        )
    if not answer.ok:
        st.error(answer.error or "provider error")
        return
    st.write(answer.text or "_(empty response)_")
    st.caption(f"{answer.latency_ms:.0f} ms")


def _render_provenance(result: AssistantResult) -> None:
    # Shared provenance: the same retrieved chunks grounded every model's answer,
    # so this maps each cited SRC back to its Task/Client (B2 contract).
    count = len(result.provenance)
    with st.expander(f"Provenance · {count} source(s) across your Tasks", expanded=False):
        if not count:
            st.caption("No sources.")
            return
        for source in result.provenance:
            st.markdown(f"- {source.label()}")
