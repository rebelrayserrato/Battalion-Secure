"""Streamlit rendering for the Cross-Task risk dashboard.

Read-only. Consumes the pure aggregation in
:mod:`review_engine.dashboard.aggregation` and draws clean, accessible charts.
Every chart is paired with a data table (a text alternative) and severity is
encoded with a colour-blind-safe palette that also separates by lightness, so
the view does not rely on colour alone.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from review_engine.dashboard.aggregation import (
    SEVERITY_ORDER,
    aggregate_findings,
    collect_records,
    is_isolation_forest_signal,
    source_refs,
)

# Colour-blind-safe severity palette (hue + lightness separation).
SEVERITY_COLORS = {
    "High": "#B42318",
    "Medium": "#B54708",
    "Low": "#175CD3",
}
_CATEGORY_COLOR = "#175CD3"


def _severity_scale():
    import altair as alt

    return alt.Scale(
        domain=SEVERITY_ORDER,
        range=[SEVERITY_COLORS[s] for s in SEVERITY_ORDER],
    )


def _category_severity_frame(summary: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for category, severities in summary["category_severity"].items():
        for severity, count in severities.items():
            rows.append({"Category": category, "Severity": severity, "Findings": count})
    return pd.DataFrame(rows)


def render_dashboard(svc) -> None:
    st.subheader("Cross-Task risk dashboard")
    st.caption(
        "Read-only rollup of findings already produced by the review pipeline "
        "across every Task, including Isolation-Forest anomaly signals. "
        "No new data is collected; all figures are review flags, not conclusions."
    )

    records = collect_records(svc.db)
    summary = aggregate_findings(records)

    if summary["total_findings"] == 0:
        st.info(
            "No findings stored yet. Process documents and run a review in a Task "
            "workspace, then return here to see the aggregated risk picture."
        )
        return

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Findings", summary["total_findings"])
    m2.metric("Tasks with findings", summary["total_tasks"])
    m3.metric("Isolation-Forest signals", summary["isolation_forest_signals"])
    m4.metric("Awaiting human review", summary["human_review_required"])

    try:
        import altair as alt
    except Exception:  # pragma: no cover - altair ships with streamlit
        alt = None

    left, right = st.columns(2)

    with left:
        st.markdown("**Findings by severity**")
        sev_frame = pd.DataFrame(
            [{"Severity": s, "Findings": c} for s, c in summary["by_severity"].items()]
        )
        if alt is not None and not sev_frame.empty:
            chart = (
                alt.Chart(sev_frame)
                .mark_bar()
                .encode(
                    x=alt.X("Findings:Q", title="Findings"),
                    y=alt.Y("Severity:N", sort=SEVERITY_ORDER, title=None),
                    color=alt.Color(
                        "Severity:N", scale=_severity_scale(), legend=None
                    ),
                    tooltip=["Severity", "Findings"],
                )
                .properties(height=140)
            )
            st.altair_chart(chart, use_container_width=True)
        st.dataframe(sev_frame, use_container_width=True, hide_index=True)

    with right:
        st.markdown("**Findings by category**")
        cat_frame = pd.DataFrame(
            [{"Category": c, "Findings": n} for c, n in summary["by_category"].items()]
        )
        if alt is not None and not cat_frame.empty:
            chart = (
                alt.Chart(cat_frame)
                .mark_bar(color=_CATEGORY_COLOR)
                .encode(
                    x=alt.X("Findings:Q", title="Findings"),
                    y=alt.Y("Category:N", sort="-x", title=None),
                    tooltip=["Category", "Findings"],
                )
                .properties(height=220)
            )
            st.altair_chart(chart, use_container_width=True)
        st.dataframe(cat_frame, use_container_width=True, hide_index=True)

    st.markdown("**Category × severity**")
    matrix = _category_severity_frame(summary)
    if alt is not None and not matrix.empty:
        chart = (
            alt.Chart(matrix)
            .mark_bar()
            .encode(
                x=alt.X("Findings:Q", stack="zero", title="Findings"),
                y=alt.Y("Category:N", sort="-x", title=None),
                color=alt.Color(
                    "Severity:N",
                    scale=_severity_scale(),
                    sort=SEVERITY_ORDER,
                    legend=alt.Legend(title="Severity"),
                ),
                order=alt.Order("Severity:N"),
                tooltip=["Category", "Severity", "Findings"],
            )
            .properties(height=260)
        )
        st.altair_chart(chart, use_container_width=True)

    st.markdown("**Top risk indicators**")
    st.caption("Recurring finding titles across Tasks, ranked by frequency.")
    indicators = summary["top_indicators"]
    if indicators:
        st.dataframe(
            [
                {
                    "Indicator": row["title"],
                    "Category": row["category"],
                    "Occurrences": row["count"],
                    "Tasks": row["tasks"],
                    "Max severity": row["max_severity"],
                    "Source refs": row["source_ref_count"],
                }
                for row in indicators
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("**Tasks by risk**")
    st.caption(
        "Risk score weights each finding by severity (High=3, Medium=2, Low=1). "
        "Expand a Task to see its findings and source references."
    )
    st.dataframe(
        [
            {
                "Task": row["matter_name"],
                "ID": row["matter_id"],
                "Findings": row["total"],
                "High": row["high"],
                "Medium": row["medium"],
                "Low": row["low"],
                "Anomaly signals": row["isolation_forest_signals"],
                "Risk score": row["risk_score"],
            }
            for row in summary["tasks"]
        ],
        use_container_width=True,
        hide_index=True,
    )

    _render_drilldown(records, summary)


def _render_drilldown(records: list[dict], summary: dict[str, Any]) -> None:
    st.markdown("**Drill down to a Task's findings**")
    by_task: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    for finding in records:
        by_task.setdefault(finding["matter_id"], []).append(finding)
        names[finding["matter_id"]] = finding.get("matter_name", finding["matter_id"])

    ordered_ids = [row["matter_id"] for row in summary["tasks"]]
    labels = {f"{names[mid]} · {mid}": mid for mid in ordered_ids}
    selection = st.selectbox(
        "Select a Task to review its findings", [""] + list(labels), index=0
    )
    matter_id = labels.get(selection)
    if not matter_id:
        return

    for finding in by_task.get(matter_id, []):
        marker = " · anomaly" if is_isolation_forest_signal(finding) else ""
        header = (
            f"{finding.get('category', '')} · {finding.get('title', '')} · "
            f"{finding.get('confidence', '')}{marker}"
        )
        with st.expander(header):
            st.write(finding.get("explanation", ""))
            reason = finding.get("confidence_reason")
            if reason:
                st.caption(f"Confidence basis: {reason}")
            sources = finding.get("supporting_sources") or []
            if sources:
                st.write("Source references:")
                for source in sources:
                    citation = source.get("citation") or source.get("source_ref")
                    st.markdown(f"- {citation}  \n  `{source.get('source_ref', '')}`")
            elif source_refs(finding):
                st.caption("Source refs: " + ", ".join(source_refs(finding)))
