from __future__ import annotations

import streamlit as st

from review_engine.app.policy_audit import PolicyAuditor
from review_engine.app.retrieval import GroundedAnswerer, make_client_scoped_retriever
from review_engine.app.services import ReviewService
from review_engine.clients.jurisdictions import (
    JURISDICTION_CHOICES,
    UNSPECIFIED_STATE,
    state_label,
)
from review_engine.law.grounding import LawGroundedAnswerer, make_law_grounded_retriever
from review_engine.law.library import (
    LAW_DISCLAIMER,
    LAW_JURISDICTION_CHOICES,
    law_jurisdiction_label,
    resolve_law_jurisdictions,
)
from review_engine.llm_connectors.ollama import OllamaConnector
from review_engine.privacy.erasure import erase_matter
from review_engine.reports.generator import generate_docx_report, generate_pdf_report
from review_engine.dashboard.view import render_dashboard
from review_engine.reports.decisions import default_decisions_path, load_decisions
from review_engine.reviewer import decisions as reviewer_decisions

st.set_page_config(
    page_title="Review Engine · RAYSERR Solutions",
    page_icon="🔎",
    layout="wide",
)

# RAYAAAA-227: brand the page as part of rayserrsolutions.com. The theme (navy
# sidebar / light content) comes from .streamlit/config.toml; this hides the
# leftover Streamlit chrome (default footer + deploy badge) so only the RAYSERR
# brand shows. The colour palette matches the admin panel's admin.css.
st.markdown(
    """
    <style>
      footer {visibility: hidden;}
      div[data-testid="stDecoration"] {display: none;}
      a[href^="https://streamlit.io"], a[href^="https://share.streamlit.io"],
      div[data-testid="stStatusWidget"] {display: none !important;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("RAYSERR Solutions · Review Engine")
st.caption(
    "Evidence-bound HR, legal-risk, and potential fraud indicator review. "
    "Local by default; human review is required."
)


@st.cache_resource
def service() -> ReviewService:
    return ReviewService()


svc = service()
with st.sidebar:
    # RAYAAAA-227: persistent link back to the admin console so the Review Engine
    # is never a dead end (Streamlit is a separate app on the auth-gated subpath;
    # this is a plain outbound link, the auth proxy / PII gate are untouched).
    st.markdown(
        "<a href='https://rayserrsolutions.com/admin' target='_top' "
        "style='display:inline-block;margin-bottom:0.75rem;color:#c8922a;"
        "font-weight:600;text-decoration:none;font-size:0.85rem;'>"
        "← Back to RAYSERR Admin</a>",
        unsafe_allow_html=True,
    )
    notice = st.session_state.pop("_deleted_notice", None)
    if notice:
        st.success(notice)
    view_mode = st.radio(
        "View",
        [
            "Task workspace",
            "Client policy library",
            "Law reference library",
            "Cross-Task risk dashboard",
            "Cross-Task assistant",
        ],
        help=(
            "The workspace reviews one Task; the policy library manages a "
            "Client's own policy corpus; the law reference library manages the "
            "per-jurisdiction statute/regulation corpus; the dashboard "
            "aggregates risk across all Tasks; the assistant answers questions "
            "ACROSS all your Tasks with your choice of model."
        ),
    )
    # RAYAAAA-244: Clients are first-class. A Task belongs to exactly one Client,
    # and jurisdiction (US state) lives on the Client. Manage clients here, then
    # pick one when creating a Task below.
    st.header("Clients")
    clients = svc.db.list_clients()
    client_label = {
        c["id"]: f"{c['display_name']} · {state_label(c['state'])}" for c in clients
    }
    if clients:
        st.caption(f"{len(clients)} client(s).")
    else:
        st.caption("No clients yet — create one below.")
    with st.expander("Create a client", expanded=not clients):
        with st.form("create_client"):
            client_name = st.text_input("Client name")
            client_state = st.selectbox(
                "Jurisdiction (US state)",
                options=JURISDICTION_CHOICES,
                index=JURISDICTION_CHOICES.index(UNSPECIFIED_STATE),
                format_func=state_label,
            )
            if st.form_submit_button("Create client", type="primary"):
                if client_name.strip():
                    new_client = svc.db.create_client(client_name, client_state)
                    st.success(f"Created client {new_client}.")
                    st.rerun()
                else:
                    st.error("Client name is required.")

    st.header("Task workspace")
    # RAYAAAA-228: show every task as a persistent list in the sidebar (instead
    # of a dropdown) so the whole workspace is visible at a glance. The radio
    # keeps the underlying matter id as its value; only the label is "Task".
    matters = svc.db.list_matters()
    name_by_id = {m["id"]: m["name"] for m in matters}
    if matters:
        matter_id = st.radio(
            "Tasks",
            options=[m["id"] for m in matters],
            format_func=lambda mid: name_by_id.get(mid, mid),
            index=0,
        )
    else:
        matter_id = None
        st.caption("No tasks yet — create one below.")
    with st.expander("Create a task", expanded=not matters):
        # RAYAAAA-228: creation needs a name only. RAYAAAA-244: a Task must be
        # attached to a Client (which carries the jurisdiction), so pick one here.
        if not clients:
            st.info("Create a client first — every Task belongs to a client.")
        else:
            with st.form("create_matter"):
                name = st.text_input("Task name")
                picked_client = st.selectbox(
                    "Client",
                    options=[c["id"] for c in clients],
                    format_func=lambda cid: client_label.get(cid, cid),
                )
                if st.form_submit_button("Create task", type="primary"):
                    if name.strip():
                        created = svc.db.create_matter(name, client_id=picked_client)
                        st.success(f"Created {created}.")
                        st.rerun()
                    else:
                        st.error("Task name is required.")

if view_mode == "Client policy library":
    # RAYAAAA-245 (Phase B): manage a Client's own uploaded HR/company policy
    # corpus. Uploaded ONCE per Client and indexed apart from any Task's docs;
    # a Task's Chat / policy-audit retrieval then composes the Task index with
    # ONLY its linked client's policy library (never another client's).
    st.subheader("Client policy library")
    st.caption(
        "Upload each Client's own HR/company policies once. They are stored and "
        "indexed separately from any Task's documents, and a Task's Chat and "
        "before-you-sign review pull from the Task's docs plus ONLY this "
        "Client's policies. Synthetic / owner-internal data only."
    )
    if not clients:
        st.info("Create a client in the sidebar first — the policy library is per-client.")
        st.stop()
    lib_client = st.selectbox(
        "Client",
        options=[c["id"] for c in clients],
        format_func=lambda cid: client_label.get(cid, cid),
        key="policy_lib_client",
    )
    policy_uploads = st.file_uploader(
        "Upload policy documents",
        type=["pdf", "docx", "txt", "csv", "xlsx", "png", "jpg", "jpeg", "zip"],
        accept_multiple_files=True,
        key="policy_uploader",
        help="Stored under this client's local policy library; not sent for model training.",
    )
    if st.button("Save policy files", disabled=not policy_uploads, key="policy_save"):
        for uploaded in policy_uploads:
            svc.save_policy_upload(lib_client, uploaded.name, uploaded.getvalue())
        st.success(f"Saved {len(policy_uploads)} policy file(s).")
        st.rerun()
    policy_docs = svc.db.list_policy_documents(lib_client)
    if policy_docs:
        st.dataframe(
            [
                {
                    "Policy document": item["name"],
                    "Type": item["file_type"],
                    "Bytes": item["size"],
                    "Indexed": item["processed_at"] or "No",
                }
                for item in policy_docs
            ],
            use_container_width=True,
            hide_index=True,
        )
        col_proc, col_del = st.columns(2)
        with col_proc:
            if st.button("Process policy library", type="primary", key="policy_process"):
                with st.spinner("Extracting and indexing this client's policies…"):
                    result = svc.process_policy_library(lib_client)
                if result["errors"]:
                    st.warning("\n".join(result["errors"]))
                st.success(
                    f"Indexed {result['processed']} policy document(s) into "
                    f"{result['chunks']} source chunks."
                )
        with col_del:
            to_delete = st.selectbox(
                "Remove a policy document",
                options=["—"] + [d["name"] for d in policy_docs],
                key="policy_delete_pick",
            )
            if st.button("Delete selected policy", disabled=to_delete == "—", key="policy_delete"):
                svc.delete_policy_document(lib_client, to_delete)
                st.success(f"Removed {to_delete} from the policy library.")
                st.rerun()
    else:
        st.info("No policy documents uploaded for this client yet.")
    st.stop()

if view_mode == "Law reference library":
    # RAYAAAA-251 (Phase C): per-JURISDICTION law corpus (statute/regulation text
    # from OFFICIAL government publishers, per the RAYAAAA-243 Counsel memo).
    # Public law, keyed by jurisdiction (state code or `federal`) — shared across
    # all clients in that state and stored entirely apart from any client/Task
    # corpus (never swept by client-data erasure). Provenance is mandatory.
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
    st.stop()

if view_mode == "Cross-Task risk dashboard":
    render_dashboard(svc)
    st.stop()

if view_mode == "Cross-Task assistant":
    # RAYAAAA-248 (Phase B3): the non-Task-scoped "personal assistant" — wires B1
    # (multi-model MCP connector) to B2 (owner-scoped cross-Task retrieval). This
    # is a SEPARATE surface from the per-Task Chat tab below; feature-flag gated
    # (CROSS_TASK_ASSISTANT_ENABLED) and synthetic-only.
    from review_engine.app.assistant_view import render_assistant

    render_assistant(svc, clients, client_label)
    st.stop()

if not matter_id:
    st.info("Create or select a task to begin.")
    st.stop()

matter = svc.db.get_matter(matter_id)
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
    # erase_matter directly (NOT the HTTP fan-out endpoint, which is for the
    # main-app client-erasure flow) — it removes the matters row plus all child
    # rows, uploads, index, and any report bytes (RAYAAAA-196, verified 0/0/0).
    # Streamlit has no native confirm dialog, so require an explicit checkbox
    # before the delete button activates.
    confirm = st.checkbox("Confirm delete", key=f"confirm_delete_{matter_id}")
    if st.button("Delete task", type="primary", disabled=not confirm):
        report = erase_matter(matter_id, svc.db.path)
        # Log at system level (matter_id=None) so the record survives the erase.
        svc.db.log("matter_deleted", None, f"{matter['name']} ({matter_id})")
        if report.clean:
            st.session_state["_deleted_notice"] = f"Deleted task {matter['name']}."
        else:
            st.session_state["_deleted_notice"] = (
                f"Deleted task {matter['name']} with residual: {report.residual_summary()}"
            )
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
            # RAYAAAA-245: compose the Task index with ONLY the linked client's
            # policy library (scoped by client id; never another client's).
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
    # RAYAAAA-233 (P2b): policy-audit / before-you-sign. Templated review over
    # the same retrieval; reuses the findings/source-reference model.
    st.caption(
        "\"Before you sign\": screens this Task's documents against this Client's "
        "own policy library plus a generic risky-clause checklist, flagging "
        "unusual/risky clauses and missing protections. Evidence-bound, "
        "local-only, and a screening aid — requires human review."
    )
    # RAYAAAA-245: build a client-specific checklist from the linked client's
    # policy library (SCOPE 3) and add it to the generic default; audit over the
    # composed Task + linked-client-policy retrieval (SCOPE 2).
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
            "generic checklist only. Add policies in the Client policy library view."
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
    # RAYAAAA-251 (Phase C): law-grounded Q&A. Composes this Task's docs + the
    # linked Client's policy library + the jurisdiction-filtered law corpus
    # ({client state} ∪ {federal} ONLY). Renders the counsel-bound disclaimer on
    # every answer, stamps each law citation with its provenance, and redacts any
    # statute citation not backed by a retrieved chunk ("not in reference library").
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
        # AC G: the exact disclaimer, on every law-grounded answer.
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
    # RAYAAAA-231 (P1b): deterministic document compare / redline between two
    # processed documents (or two versions) in this matter. The diff itself is
    # local + model-free (difflib over the existing SourceChunk model); the
    # optional plain-language summary reuses the bounded local-Ollama connector
    # and degrades gracefully when it is unavailable. No egress.
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
        default_compare = 1 if processed_docs[1] != base_name else 0
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

    # Collect the reviewable source chunks (SRC IDs), keyed by chunk id. Chunks
    # cited by a finding carry the finding context; optionally include every
    # indexed chunk so nothing is unreachable for review.
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
            "approved": "✅ Approved",
            "rejected": "❌ Rejected",
            "needs_changes": "✏️ Needs changes",
            "undecided": "⏳ Undecided",
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
