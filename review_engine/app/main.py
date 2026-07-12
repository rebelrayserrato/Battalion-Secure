from __future__ import annotations

import streamlit as st

from review_engine.app.services import ReviewService
from review_engine.llm_connectors.ollama import OllamaConnector
from review_engine.reports.generator import generate_docx_report, generate_pdf_report

st.set_page_config(page_title="Local Evidence Review", page_icon="🔎", layout="wide")
st.title("Local Evidence Review")
st.caption(
    "Evidence-bound HR, legal-risk, and potential fraud indicator review. "
    "Local by default; human review is required."
)


@st.cache_resource
def service() -> ReviewService:
    return ReviewService()


svc = service()
with st.sidebar:
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
        "Compare",
    ]
)

with tabs[0]:
    uploads = st.file_uploader(
        "Upload original documents",
        type=["pdf", "docx", "txt", "csv", "xlsx", "png", "jpg", "jpeg", "zip"],
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

with tabs[7]:
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
