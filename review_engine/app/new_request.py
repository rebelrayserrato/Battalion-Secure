"""RAYAAAA-264: the "New Review Request" 2-step wizard.

Owner ask (RAYAAAA-191): make the Review Engine flow like the base44 'Aich-R'
demo. Step 1 lets the owner pick a *review type*; step 2 uploads an (optional)
document and a free-text question, then submits.

The review "types" are PRESETS over the EXISTING review pipeline — there is no
new engine. Each preset only decides which of the already-shipped, local,
evidence-bound capabilities run for a submission:
  * ``run_reviews`` (HR/legal + potential-fraud, RAYAAAA-192/legal_hr_review +
    fraud_detection),
  * the "before you sign" policy audit (RAYAAAA-233), and
  * a grounded / law-grounded answer to the owner's question (RAYAAAA-232 /
    RAYAAAA-251), scoped to the linked Client + jurisdiction.

A submission creates a Task (matter) under a selected Client exactly the way the
sidebar "Create a task" form does (RAYAAAA-244), so client/jurisdiction scoping
is untouched. Everything is local; no egress.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field

import streamlit as st

from review_engine.app.policy_audit import DEFAULT_CHECKLIST, PolicyAuditor, checklist_from_policies
from review_engine.app.retrieval import GroundedAnswerer, make_client_scoped_retriever
from review_engine.app.review_types import REVIEW_TYPES as _CATALOG, REVIEW_TYPES_BY_KEY as _CATALOG_BY_KEY
from review_engine.clients.jurisdictions import UNSPECIFIED_STATE, state_label


@dataclass(frozen=True)
class ReviewType:
    """A New-Request preset that maps onto the existing review pipeline.

    Title / icon / accent come from the SHARED catalogue
    (``review_engine.app.review_types``) that the RAYAAAA-263 Dashboard "Start a
    Review" cards also use, so the wizard and the dashboard never diverge and a
    dashboard card's ``nr_type`` prefilter always lands a matching wizard card.
    This module only adds the wizard-specific copy (longer description, the
    feature-tag chips) and the pipeline flags that select which EXISTING local
    capabilities run on submit.
    """

    key: str
    title: str
    icon: str  # emoji, shown in the card's coloured tile
    accent: str  # hex, tints the tile + chips
    description: str
    features: tuple[str, ...]  # feature-tag chips
    # Which existing capabilities this preset runs on submit:
    include_hr: bool = True
    include_fraud: bool = True
    run_policy_audit: bool = False
    law_grounded: bool = False


# Wizard-only extension of the shared catalogue, keyed by the SAME keys: the long
# description + feature-tag chips + the pipeline flags mapping each type onto the
# existing local review capabilities (no new backend).
_PRESETS: dict[str, dict] = {
    "legal_case": dict(
        description=(
            "Full paralegal-style case analysis: timelines, parties involved, "
            "key facts, legal issues, and research."
        ),
        features=(
            "Case file review", "Build case timelines", "Identify all parties",
            "Key facts extraction", "Legal issue spotting",
        ),
        include_hr=True, include_fraud=True,
    ),
    "hr_termination": dict(
        description=(
            "Verify termination documents meet company policy, state/federal "
            "law, and flag employment risks."
        ),
        features=(
            "Termination letter compliance", "Policy violation check",
            "WARN Act screening", "Final pay review", "Retaliation risk",
        ),
        include_hr=True, include_fraud=False, run_policy_audit=True, law_grounded=True,
    ),
    "contract": dict(
        description=(
            "Identify unfavorable clauses, missing protections, liability "
            "exposure, and negotiation points."
        ),
        features=(
            "Vendor contracts", "Employment agreements", "NDAs",
            "Liability exposure", "Negotiation points",
        ),
        include_hr=False, include_fraud=False, run_policy_audit=True,
    ),
    "compliance_audit": dict(
        description=(
            "Audit documents against regulatory frameworks: OSHA, HIPAA, SOX, "
            "GDPR, and more."
        ),
        features=(
            "HIPAA compliance", "OSHA safety audits", "GDPR data review",
            "SOX controls", "Framework mapping",
        ),
        include_hr=True, include_fraud=False, run_policy_audit=True, law_grounded=True,
    ),
    "incident_misconduct": dict(
        description=(
            "Investigate incident reports, identify misconduct patterns, assess "
            "liability, and document findings."
        ),
        features=(
            "Workplace incidents", "Harassment complaints", "Safety violations",
            "Liability assessment", "Findings documentation",
        ),
        include_hr=True, include_fraud=True,
    ),
    "general_document": dict(
        description=(
            "All-purpose document analysis with policy matching, fraud "
            "detection, and key insight extraction."
        ),
        features=(
            "Policy Q&A", "Document summarization", "Risk identification",
            "Key insight extraction",
        ),
        include_hr=True, include_fraud=True,
    ),
}


def _build_types() -> tuple[ReviewType, ...]:
    """Compose the shared catalogue (order/key/title/icon/color) with the
    wizard presets. Fails loudly if the two ever drift out of key-sync."""
    out = []
    for cat in _CATALOG:
        preset = dict(_PRESETS[cat.key])  # KeyError here = catalogue/preset drift
        preset["features"] = tuple(preset["features"])
        out.append(
            ReviewType(key=cat.key, title=cat.title, icon=cat.icon, accent=cat.color, **preset)
        )
    return tuple(out)


REVIEW_TYPES: tuple[ReviewType, ...] = _build_types()

_BY_KEY = {rt.key: rt for rt in REVIEW_TYPES}


def review_type(key: str) -> ReviewType | None:
    return _BY_KEY.get(key)


def _chips_html(rt: ReviewType, shown: int = 3) -> str:
    """Feature-tag chips for a card: first ``shown`` + a '+N more' pill."""
    visible = rt.features[:shown]
    extra = len(rt.features) - len(visible)
    tint = rt.accent
    pills = "".join(
        f"<span class='nr-chip' style='background:{tint}1a;color:{tint};'>"
        f"{html.escape(f)}</span>"
        for f in visible
    )
    if extra > 0:
        pills += f"<span class='nr-chip nr-chip-more'>+{extra} more</span>"
    return f"<div class='nr-chips'>{pills}</div>"


def _stepper_html(step: int) -> str:
    """The '1 Select Review Type ---- 2 Upload & Submit' progress header."""
    def dot(n: int, label: str) -> str:
        done = step > n
        active = step == n
        cls = "done" if done else ("active" if active else "idle")
        mark = "✓" if done else str(n)
        return (
            f"<div class='nr-step {cls}'>"
            f"<span class='nr-step-dot'>{mark}</span>"
            f"<span class='nr-step-label'>{label}</span></div>"
        )

    line_cls = "done" if step > 1 else "idle"
    return (
        "<div class='nr-stepper'>"
        + dot(1, "Select Review Type")
        + f"<div class='nr-step-line {line_cls}'></div>"
        + dot(2, "Upload &amp; Submit")
        + "</div>"
    )


_WIZARD_CSS = """
<style>
  .nr-stepper{display:flex;align-items:center;gap:.6rem;margin:.4rem 0 1.4rem;}
  .nr-step{display:flex;align-items:center;gap:.5rem;white-space:nowrap;}
  .nr-step-line{flex:1 1 auto;height:2px;background:var(--rs-border,#e4e9f0);border-radius:2px;}
  .nr-step-line.done{background:var(--rs-teal,#2a9d8f);}
  .nr-step-dot{display:inline-flex;align-items:center;justify-content:center;
    width:26px;height:26px;border-radius:50%;font-size:.8rem;font-weight:700;
    background:#e4e9f0;color:#64748b;}
  .nr-step.active .nr-step-dot,.nr-step.done .nr-step-dot{background:var(--rs-teal,#2a9d8f);color:#fff;}
  .nr-step-label{font-weight:600;color:#64748b;font-size:.95rem;}
  .nr-step.active .nr-step-label,.nr-step.done .nr-step-label{color:var(--rs-teal,#2a9d8f);}
  .nr-card{padding:.15rem .1rem .35rem;}
  .nr-card-head{display:flex;align-items:flex-start;gap:.7rem;}
  .nr-tile{flex:0 0 auto;width:42px;height:42px;border-radius:11px;display:flex;
    align-items:center;justify-content:center;font-size:1.25rem;}
  .nr-card-title{font-weight:700;color:var(--rs-navy,#1b2f5b);font-size:1.02rem;line-height:1.2;}
  .nr-card-desc{color:#475569;font-size:.86rem;line-height:1.35;margin:.45rem 0 .1rem;}
  .nr-chips{display:flex;flex-wrap:wrap;gap:.35rem;margin-top:.55rem;}
  .nr-chip{font-size:.72rem;font-weight:600;padding:.16rem .5rem;border-radius:999px;
    white-space:nowrap;}
  .nr-chip-more{background:transparent;color:#94a3b8;font-weight:500;}
  /* Highlight the whole bordered card when its (hidden) marker is present. */
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.nr-card-selected){
    border-color:var(--rs-teal,#2a9d8f) !important;
    box-shadow:0 0 0 1px var(--rs-teal,#2a9d8f), 0 4px 20px rgba(42,157,143,.12) !important;
    background:#f2fbf9;}
  .nr-selected-flag{color:var(--rs-teal,#2a9d8f);font-weight:700;font-size:.8rem;}
</style>
"""


def _render_card(rt: ReviewType, selected: bool) -> None:
    """One selectable review-type card."""
    with st.container(border=True):
        marker = "<span class='nr-card-selected'></span>" if selected else ""
        flag = "<span class='nr-selected-flag'>✓ Selected</span>" if selected else ""
        st.markdown(
            f"{marker}"
            "<div class='nr-card'><div class='nr-card-head'>"
            f"<div class='nr-tile' style='background:{rt.accent}1a;'>{rt.icon}</div>"
            "<div><div class='nr-card-title'>"
            f"{html.escape(rt.title)} {flag}</div>"
            f"<div class='nr-card-desc'>{html.escape(rt.description)}</div>"
            "</div></div>"
            f"{_chips_html(rt)}</div>",
            unsafe_allow_html=True,
        )
        label = "✓ Selected" if selected else "Select"
        if st.button(
            label,
            key=f"nr_pick_{rt.key}",
            use_container_width=True,
            type="primary" if selected else "secondary",
        ):
            st.session_state["nr_type"] = rt.key
            st.rerun()


def _run_submission(svc, matter_id: str, rt: ReviewType, question: str) -> dict:
    """Run the preset's existing-pipeline steps for a fresh submission.

    Process the uploaded document, run the preset's reviews, optionally the
    before-you-sign policy audit, and answer the owner's question over the
    client-scoped retrieval. All local and evidence-bound.
    """
    result: dict = {"errors": []}
    proc = svc.process_matter(matter_id)
    result["processed"] = proc["processed"]
    result["chunks"] = proc["chunks"]
    result["errors"].extend(proc.get("errors", []))

    result["findings"] = svc.run_reviews(matter_id, rt.include_hr, rt.include_fraud)

    if rt.run_policy_audit:
        matter = svc.db.get_matter(matter_id) or {}
        client_id = matter.get("client_id")
        policy_chunks = svc.db.get_policy_chunks(client_id) if client_id else []
        checklist = checklist_from_policies(policy_chunks) + DEFAULT_CHECKLIST
        auditor = PolicyAuditor(retriever=make_client_scoped_retriever(svc.db))
        result["audit_findings"] = auditor.audit(matter_id, checklist=checklist)

    if question.strip():
        answerer = GroundedAnswerer(retriever=make_client_scoped_retriever(svc.db))
        result["answer"] = answerer.answer(matter_id, question)
    return result


def render_new_request(svc, clients: list[dict], client_label: dict) -> None:
    """Render the two-step New Review Request wizard."""
    st.markdown(_WIZARD_CSS, unsafe_allow_html=True)
    selected_key = st.session_state.get("nr_type")
    rt = review_type(selected_key) if selected_key else None
    step = st.session_state.get("nr_step", 1)
    if rt is None:
        step = 1

    # --- Step 2 header shows the chosen type; step 1 is the generic prompt. ---
    if step == 2 and rt is not None:
        st.title("New Review Request")
        st.caption(f"{rt.title} — Upload your documents and provide context")
    else:
        st.title("New Review Request")
        st.caption("Choose the type of review you need")
    st.markdown(_stepper_html(step), unsafe_allow_html=True)

    if step == 2 and rt is not None:
        _render_step_two(svc, rt, clients, client_label)
        return

    # --- Step 1: pick a review type -----------------------------------------
    for i in range(0, len(REVIEW_TYPES), 2):
        cols = st.columns(2)
        for col, card in zip(cols, REVIEW_TYPES[i : i + 2]):
            with col:
                _render_card(card, selected=(card.key == selected_key))

    st.write("")
    cta = f"Continue with {rt.title}  →" if rt else "Select a review type to continue"
    if st.button(cta, type="primary", use_container_width=True, disabled=rt is None):
        st.session_state["nr_step"] = 2
        st.rerun()


def _render_step_two(svc, rt: ReviewType, clients: list[dict], client_label: dict) -> None:
    if st.button("← Change review type", key="nr_back"):
        st.session_state["nr_step"] = 1
        st.rerun()

    if not clients:
        st.info(
            "Create a client in the sidebar first — every request belongs to a "
            "client (which carries the jurisdiction)."
        )
        return

    picked_client = st.selectbox(
        "Client",
        options=[c["id"] for c in clients],
        format_func=lambda cid: client_label.get(cid, cid),
        key="nr_client",
    )
    default_name = f"{rt.title}"
    task_name = st.text_input("Request name", value=default_name, key="nr_task_name")

    st.markdown("##### \U0001f4e4 Upload Document  *(optional)*")
    upload = st.file_uploader(
        "Drop your document here or click to browse",
        type=["pdf", "docx", "txt", "csv", "xlsx", "png", "jpg", "jpeg", "zip"],
        accept_multiple_files=False,
        key="nr_upload",
        help="PDF, Word, images, and text files supported. Stored locally; not sent for model training.",
        label_visibility="collapsed",
    )

    st.markdown("##### \U0001f4ac Your Question")
    question = st.text_area(
        "Your Question",
        key="nr_question",
        placeholder="e.g. 'Summarize the timeline and identify all defendants in this case file…'",
        label_visibility="collapsed",
        height=120,
    )

    if st.button(f"Submit for {rt.title}", type="primary", use_container_width=True, key="nr_submit"):
        if not task_name.strip():
            st.error("A request name is required.")
            return
        with st.spinner("Creating the request, indexing evidence, and running the review…"):
            # Stamp the review type into the matter description, matching the
            # RAYAAAA-263 shell's create convention so "My Requests" shows it.
            matter_id = svc.db.create_matter(
                task_name.strip(),
                description=f"Review type: {rt.title}",
                client_id=picked_client,
            )
            if upload is not None:
                svc.save_upload(matter_id, upload.name, upload.getvalue())
            svc.db.log("new_request_submitted", matter_id, f"type={rt.key}")
            outcome = _run_submission(svc, matter_id, rt, question or "")
        st.session_state["nr_last_result"] = {"matter_id": matter_id, "type": rt.key, "outcome": outcome}
        # Make the new request the active one so the RAYAAAA-263 shell's
        # "My Requests"/Task workspace opens straight to it.
        st.session_state["active_matter_id"] = matter_id
        st.rerun()

    _render_result(svc, rt)


def _render_result(svc, rt: ReviewType) -> None:
    payload = st.session_state.get("nr_last_result")
    if not payload or payload.get("type") != rt.key:
        return
    outcome = payload["outcome"]
    matter_id = payload["matter_id"]
    st.divider()
    st.success(
        f"Request submitted as Task **{matter_id}**. "
        f"Indexed {outcome.get('chunks', 0)} source chunk(s). Requires human review."
    )
    if outcome.get("errors"):
        st.warning("\n".join(outcome["errors"]))

    answer = outcome.get("answer")
    if answer:
        st.markdown("#### Answer to your question")
        st.write(answer["answer"])
        if answer.get("sources"):
            st.caption("Sources: " + ", ".join(s["citation"] for s in answer["sources"]))
        if not answer.get("model_used"):
            st.info("Local model unavailable — showed retrieved passages only.")

    findings = outcome.get("findings") or []
    if findings:
        st.markdown(f"#### Review findings ({len(findings)})")
        for finding in findings:
            with st.expander(f"{finding['category']} · {finding['title']} · {finding['confidence']}"):
                st.write(finding["explanation"])
                for source in finding.get("supporting_sources", []):
                    st.markdown(f"- {source['citation']}")

    audit = outcome.get("audit_findings")
    if audit:
        st.markdown(f"#### Before-you-sign items ({len(audit)})")
        for finding in audit:
            with st.expander(f"{finding['category']} · {finding['title']} · {finding['confidence']}"):
                st.write(finding["explanation"])
                for source in finding.get("supporting_sources", []):
                    st.markdown(f"- {source['citation']}")

    st.caption(
        "Open the full Task workspace from the sidebar (**Task workspace** view) to "
        "explore documents, timeline, chat, compare, and export the branded report."
    )
