from review_engine.dashboard.aggregation import (
    aggregate_findings,
    collect_records,
    is_isolation_forest_signal,
    severity_of,
    source_refs,
)


def _finding(matter_id, matter_name, title, category, confidence, explanation="", refs=(), review=True):
    return {
        "matter_id": matter_id,
        "matter_name": matter_name,
        "title": title,
        "category": category,
        "confidence": confidence,
        "explanation": explanation,
        "human_review_required": review,
        "supporting_sources": [{"source_ref": r, "citation": r} for r in refs],
    }


def _sample_records():
    return [
        _finding(
            "MAT-A", "Alpha", "Unusual payment amount", "Fraud Red Flag", "Medium",
            explanation="Amount $9,000.00 on row 4 received an Isolation Forest anomaly score of 0.71.",
            refs=["MAT-A::ledger.csv::row4"],
        ),
        _finding(
            "MAT-A", "Alpha", "Potential duplicate invoices", "Fraud Red Flag", "High",
            refs=["MAT-A::ledger.csv::row2", "MAT-A::ledger.csv::row3"],
        ),
        _finding(
            "MAT-A", "Alpha", "Missing or incomplete approval", "Fraud Red Flag", "High",
            refs=["MAT-A::ledger.csv::row5"],
        ),
        _finding(
            "MAT-B", "Bravo", "Unusual payment amount", "Fraud Red Flag", "Medium",
            explanation="Amount $12,500.00 on row 8 received an Isolation Forest anomaly score of 0.63.",
            refs=["MAT-B::pay.xlsx::row8"],
        ),
        _finding(
            "MAT-B", "Bravo", "Termination timing risk", "HR Legal Risk", "Low",
            refs=["MAT-B::memo.txt::p1"], review=False,
        ),
    ]


def test_severity_normalisation_defaults_to_low():
    assert severity_of({"confidence": "high"}) == "High"
    assert severity_of({"confidence": "  medium "}) == "Medium"
    assert severity_of({"confidence": "bogus"}) == "Low"
    assert severity_of({}) == "Low"


def test_isolation_forest_detection():
    assert is_isolation_forest_signal(
        {"explanation": "received an Isolation Forest anomaly score of 0.5"}
    )
    assert not is_isolation_forest_signal({"explanation": "duplicate invoice identifier"})
    assert not is_isolation_forest_signal({})


def test_source_refs_extracts_ids_and_ignores_malformed():
    finding = {"supporting_sources": [{"source_ref": "R1"}, {"citation": "no ref"}, "junk"]}
    assert source_refs(finding) == ["R1"]


def test_aggregate_totals_and_axes():
    summary = aggregate_findings(_sample_records())
    assert summary["total_findings"] == 5
    assert summary["total_tasks"] == 2
    # Two Isolation-Forest anomaly findings across the two Tasks.
    assert summary["isolation_forest_signals"] == 2
    # Four findings require human review (one HR finding set review=False).
    assert summary["human_review_required"] == 4
    assert summary["findings_with_sources"] == 5
    assert summary["by_category"]["Fraud Red Flag"] == 4
    assert summary["by_category"]["HR Legal Risk"] == 1
    assert summary["by_severity"] == {"High": 2, "Medium": 2, "Low": 1}


def test_category_severity_matrix():
    summary = aggregate_findings(_sample_records())
    fraud = summary["category_severity"]["Fraud Red Flag"]
    assert fraud == {"High": 2, "Medium": 2}
    assert summary["category_severity"]["HR Legal Risk"] == {"Low": 1}


def test_task_rollup_ranked_by_risk_score():
    summary = aggregate_findings(_sample_records())
    tasks = summary["tasks"]
    assert [t["matter_id"] for t in tasks] == ["MAT-A", "MAT-B"]
    alpha = tasks[0]
    # High(3)+High(3)+Medium(2) = 8
    assert alpha["risk_score"] == 8
    assert alpha["total"] == 3
    assert alpha["high"] == 2
    assert alpha["isolation_forest_signals"] == 1
    bravo = tasks[1]
    # Medium(2)+Low(1) = 3
    assert bravo["risk_score"] == 3
    assert bravo["human_review_required"] == 1


def test_top_indicators_ranked_and_deduped_across_tasks():
    summary = aggregate_findings(_sample_records())
    top = summary["top_indicators"]
    first = top[0]
    assert first["title"] == "Unusual payment amount"
    assert first["count"] == 2
    assert first["tasks"] == 2
    assert first["max_severity"] == "Medium"
    assert first["source_ref_count"] == 2


def test_top_n_limit():
    records = [
        _finding("MAT-A", "Alpha", f"Indicator {i}", "Fraud Red Flag", "Low", refs=["r"])
        for i in range(20)
    ]
    summary = aggregate_findings(records, top_n=5)
    assert len(summary["top_indicators"]) == 5


def test_empty_input_is_safe():
    summary = aggregate_findings([])
    assert summary["total_findings"] == 0
    assert summary["total_tasks"] == 0
    assert summary["by_category"] == {}
    assert summary["by_severity"] == {}
    assert summary["tasks"] == []
    assert summary["top_indicators"] == []


def test_missing_fields_do_not_crash():
    summary = aggregate_findings([{"category": "Fraud Red Flag"}])
    assert summary["total_findings"] == 1
    # Defaults: unassigned Task, Low severity, untitled indicator.
    assert summary["total_tasks"] == 1
    assert summary["by_severity"] == {"Low": 1}
    assert summary["top_indicators"][0]["title"] == "Untitled finding"


class _FakeDB:
    def __init__(self, matters, findings_by_matter):
        self._matters = matters
        self._findings = findings_by_matter

    def list_matters(self):
        return self._matters

    def get_findings(self, matter_id):
        return self._findings.get(matter_id, [])


def test_collect_records_annotates_task_identity_read_only():
    db = _FakeDB(
        matters=[{"id": "MAT-A", "name": "Alpha"}, {"id": "MAT-B", "name": "Bravo"}],
        findings_by_matter={
            "MAT-A": [{"title": "x", "category": "Fraud Red Flag", "confidence": "High"}],
            "MAT-B": [],
        },
    )
    records = collect_records(db)
    assert len(records) == 1
    assert records[0]["matter_id"] == "MAT-A"
    assert records[0]["matter_name"] == "Alpha"
    # Round-trips cleanly through the aggregator.
    summary = aggregate_findings(records)
    assert summary["total_findings"] == 1
    assert summary["total_tasks"] == 1
