from __future__ import annotations

import streamlit as st

from review_engine.app.services import ReviewService
from review_engine.llm_connectors.ollama import OllamaConnector
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
    st.header("Matter workspace")
    matters = svc.db.list_matters()
    labels = {f"{m['name']} · {m['id']}": m["id"] for m in matters}
    selection = st.selectbox("Select matter", [""] + list(labels), index=0)
    matter_id = labels.get(selection)
    with st.expander("Create a matter", expanded=not matters):
        with st.form("create_matter"):
            name = st.text_input("Matter name")
            description = st.text_area("Description")
            jurisdiction = st.text_input("Jurisdiction (optional)")
            if st.form_submit_button("Create matter", type="primary"):
                if name.strip():
                    created = svc.db.create_matter(name, description, jurisdiction)
                    st.success(f"Created {created}. Select it above.")
                    st.rerun()
                else:
                    st.error("Matter name is required.")

if not matter_id:
    st.info("Create or select a matter to begin.")
    st.stop()

matter = svc.db.get_matter(matter_id)
st.subheader(f"{matter['name']} · {matter_id}")
if not matter.get("jurisdiction"):
    st.warning("Jurisdiction required for jurisdiction-dependent legal review.")

tabs = st.tabs(
    [
        "Documents",
        "Search evidence",
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
        help="Files stay under this local matter workspace and are not sent for model training.",
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

with tabs[6]:
    logs = svc.db.get_audit_log(matter_id)
    st.dataframe(logs, use_container_width=True, hide_index=True)
