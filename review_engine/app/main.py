from __future__ import annotations

import streamlit as st

from review_engine.app.rag_chat import RagChatService
from review_engine.app.services import ReviewService
from review_engine.llm_connectors.ollama import OllamaConnector
from review_engine.privacy.erasure import erase_matter
from review_engine.reports.generator import generate_docx_report, generate_pdf_report

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
        # RAYAAAA-228: creation needs a name only. Description/jurisdiction still
        # default to empty in create_matter and can be added later; the owner's
        # ask is that starting a task requires nothing but a name.
        with st.form("create_matter"):
            name = st.text_input("Task name")
            if st.form_submit_button("Create task", type="primary"):
                if name.strip():
                    created = svc.db.create_matter(name)
                    st.success(f"Created {created}.")
                    st.rerun()
                else:
                    st.error("Task name is required.")

if not matter_id:
    st.info("Create or select a task to begin.")
    st.stop()

matter = svc.db.get_matter(matter_id)
header_col, delete_col = st.columns([5, 1])
with header_col:
    st.subheader(f"{matter['name']} · {matter_id}")
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
if not matter.get("jurisdiction"):
    st.warning("Jurisdiction required for jurisdiction-dependent legal review.")

tabs = st.tabs(
    [
        "Documents",
        "Search evidence",
        "Chat",
        "Run review",
        "Timeline",
        "Findings",
        "Export report",
        "Audit log",
    ]
)

with tabs[0]:
    uploads = st.file_uploader(
        "Upload original documents",
        type=["pdf", "docx", "txt", "csv", "xlsx"],
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
    # RAYAAAA-232: grounded RAG chat. The answer is retrieval-augmented over this
    # Task's local Chroma index and cites source-reference IDs. Same guardrails as
    # the summarizer: answers come ONLY from retrieved chunks, no new facts / legal
    # conclusions, and it degrades gracefully when Ollama is unavailable (verbatim
    # source excerpts instead). All inference is local — no external API / egress.
    st.caption(
        "Answers are drawn only from this Task's processed documents and cite "
        "source-reference IDs. Not legal advice — human review is required."
    )
    chat_ollama = st.checkbox(
        "Use local Ollama to draft the answer", value=False, key="chat_use_ollama",
        help="Unchecked (or if Ollama is offline) shows the most relevant source excerpts verbatim.",
    )
    chat_model = (
        st.text_input("Ollama model", value="llama3.2", key="chat_model")
        if chat_ollama
        else None
    )
    chat_k = st.slider("Passages to retrieve", 1, 12, 6, key="chat_top_k")
    question = st.text_input(
        "Ask a question about this Task's documents",
        placeholder="What date was the contract terminated? Who approved invoice 1042?",
        key="chat_question",
    )
    if st.button("Ask", type="primary", disabled=not question.strip()):
        connector = OllamaConnector(model=chat_model) if chat_ollama else None
        try:
            chat_service = RagChatService.for_matter(matter_id, connector=connector)
            with st.spinner("Retrieving grounded evidence…"):
                result = chat_service.answer(question, limit=chat_k)
            svc.db.log("chat_query", matter_id, f"{len(result.sources)} source(s); model={result.model_used}")
            if result.notice:
                st.info(result.notice)
            if not result.grounded:
                st.warning(result.text)
            else:
                st.markdown(result.text)
                st.caption("Cited sources:")
                for source in result.sources:
                    with st.expander(source.citation):
                        st.write(source.text)
                        st.caption(f"Source: {source.source_ref}")
        except Exception as exc:
            st.error(f"Chat unavailable: {exc}. Process the documents first.")

with tabs[3]:
    include_hr = st.checkbox("HR / legal risk review", value=True)
    include_fraud = st.checkbox("Potential fraud indicator review", value=True)
    st.caption("Rules and anomaly scores identify review flags, not legal conclusions or proof of fraud.")
    if st.button("Run selected reviews", type="primary"):
        findings = svc.run_reviews(matter_id, include_hr, include_fraud)
        st.success(f"Review complete: {len(findings)} source-supported finding(s).")

with tabs[4]:
    timeline = svc.timeline(matter_id)
    if timeline:
        st.dataframe(timeline, use_container_width=True, hide_index=True)
    else:
        st.info("No dated events identified in processed evidence.")

with tabs[5]:
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

with tabs[6]:
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
    col1, col2 = st.columns(2)
    with col1:
        docx = generate_docx_report(svc.db, matter_id, executive_summary=summary)
        if st.download_button(
            "Download DOCX report", docx, f"{matter_id}_review_report.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ):
            svc.db.log("report_generated", matter_id, "DOCX")
    with col2:
        pdf = generate_pdf_report(svc.db, matter_id, executive_summary=summary)
        if st.download_button(
            "Download PDF report", pdf, f"{matter_id}_review_report.pdf", "application/pdf"
        ):
            svc.db.log("report_generated", matter_id, "PDF")

with tabs[7]:
    logs = svc.db.get_audit_log(matter_id)
    st.dataframe(logs, use_container_width=True, hide_index=True)
