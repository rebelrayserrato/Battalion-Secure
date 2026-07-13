"""Cross-Task risk aggregation.

Pure standard-library functions that summarise the findings already stored by
the review pipeline across every Task (matter). No new data is collected and no
inference is performed here -- this is a read-only rollup of existing records,
including the Isolation-Forest anomaly findings produced by
:mod:`review_engine.fraud_detection.review`.

Terminology: the workspace UI relabels a "matter" as a "Task"; the storage layer
still keys on ``matter_id``. Both terms refer to the same unit of work.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

# Confidence is the severity axis carried on every stored finding.
SEVERITY_ORDER = ["High", "Medium", "Low"]
SEVERITY_WEIGHT = {"High": 3, "Medium": 2, "Low": 1}
_UNKNOWN_SEVERITY = "Low"

# Categories the pipeline can assign (mirrors evidence.findings.VALID_CATEGORIES).
CATEGORY_ORDER = [
    "Fraud Red Flag",
    "HR Legal Risk",
    "Contradiction",
    "Timeline Issue",
    "Missing Document",
    "Unsupported Finding",
]

# Marker text written by the Isolation-Forest branch of the fraud reviewer.
_ISOLATION_FOREST_MARKER = "isolation forest"


def severity_of(finding: dict) -> str:
    """Return a normalised severity label for a stored finding."""
    confidence = str(finding.get("confidence", "") or "").strip().title()
    return confidence if confidence in SEVERITY_WEIGHT else _UNKNOWN_SEVERITY


def is_isolation_forest_signal(finding: dict) -> bool:
    """True when a finding originated from the Isolation-Forest anomaly branch.

    The fraud reviewer records the phrase "Isolation Forest anomaly score" in the
    explanation of anomaly findings; we match on that marker so the dashboard can
    surface the model-derived signals distinctly from rule-based flags.
    """
    explanation = str(finding.get("explanation", "") or "").lower()
    return _ISOLATION_FOREST_MARKER in explanation


def source_refs(finding: dict) -> list[str]:
    """Extract the source-reference IDs backing a finding (may be empty)."""
    refs: list[str] = []
    for source in finding.get("supporting_sources") or []:
        ref = source.get("source_ref") if isinstance(source, dict) else None
        if ref:
            refs.append(str(ref))
    return refs


@dataclass
class TaskRisk:
    """Per-Task rollup used for the drill-down table and links."""

    matter_id: str
    matter_name: str
    total: int = 0
    by_severity: Counter = field(default_factory=Counter)
    by_category: Counter = field(default_factory=Counter)
    isolation_forest_signals: int = 0
    human_review_required: int = 0
    risk_score: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "matter_id": self.matter_id,
            "matter_name": self.matter_name,
            "total": self.total,
            "high": self.by_severity.get("High", 0),
            "medium": self.by_severity.get("Medium", 0),
            "low": self.by_severity.get("Low", 0),
            "isolation_forest_signals": self.isolation_forest_signals,
            "human_review_required": self.human_review_required,
            "risk_score": self.risk_score,
        }


@dataclass
class RiskIndicator:
    """A recurring finding title, ranked across Tasks."""

    title: str
    category: str
    count: int
    tasks: int
    max_severity: str
    source_ref_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "category": self.category,
            "count": self.count,
            "tasks": self.tasks,
            "max_severity": self.max_severity,
            "source_ref_count": self.source_ref_count,
        }


def _ordered_counter(counter: Counter, order: list[str]) -> dict[str, int]:
    """Return counts following ``order`` first, then any extras by count desc."""
    result: dict[str, int] = {}
    for key in order:
        if counter.get(key):
            result[key] = counter[key]
    for key, value in counter.most_common():
        if key not in result and value:
            result[key] = value
    return result


def _max_severity(labels: Iterable[str]) -> str:
    best = _UNKNOWN_SEVERITY
    best_weight = 0
    for label in labels:
        weight = SEVERITY_WEIGHT.get(label, 0)
        if weight > best_weight:
            best_weight = weight
            best = label
    return best


def aggregate_findings(
    records: list[dict], *, top_n: int = 10
) -> dict[str, Any]:
    """Summarise stored findings across Tasks.

    ``records`` is a flat list of finding dicts (as returned by
    ``ReviewDatabase.get_findings``) each annotated with ``matter_id`` and
    ``matter_name``. Returns a JSON-serialisable summary with counts by category
    and severity, per-Task rollups, and the top recurring risk indicators.
    """
    by_category: Counter = Counter()
    by_severity: Counter = Counter()
    category_severity: dict[str, Counter] = defaultdict(Counter)
    tasks: dict[str, TaskRisk] = {}
    indicator_counts: Counter = Counter()
    indicator_meta: dict[str, dict[str, Any]] = {}

    total = 0
    isolation_forest_total = 0
    human_review_total = 0
    findings_with_sources = 0

    for finding in records:
        total += 1
        matter_id = str(finding.get("matter_id", "") or "unassigned")
        matter_name = str(finding.get("matter_name", "") or matter_id)
        category = str(finding.get("category", "") or "Unsupported Finding")
        severity = severity_of(finding)
        refs = source_refs(finding)
        is_if = is_isolation_forest_signal(finding)
        needs_review = bool(finding.get("human_review_required", False))

        by_category[category] += 1
        by_severity[severity] += 1
        category_severity[category][severity] += 1
        if refs:
            findings_with_sources += 1
        if is_if:
            isolation_forest_total += 1
        if needs_review:
            human_review_total += 1

        task = tasks.get(matter_id)
        if task is None:
            task = TaskRisk(matter_id=matter_id, matter_name=matter_name)
            tasks[matter_id] = task
        task.total += 1
        task.by_severity[severity] += 1
        task.by_category[category] += 1
        task.risk_score += SEVERITY_WEIGHT.get(severity, 0)
        if is_if:
            task.isolation_forest_signals += 1
        if needs_review:
            task.human_review_required += 1

        title = str(finding.get("title", "") or "Untitled finding")
        indicator_counts[title] += 1
        meta = indicator_meta.setdefault(
            title,
            {"category": category, "tasks": set(), "severities": [], "source_refs": 0},
        )
        meta["tasks"].add(matter_id)
        meta["severities"].append(severity)
        meta["source_refs"] += len(refs)

    task_rollup = sorted(
        (task.as_dict() for task in tasks.values()),
        key=lambda row: (row["risk_score"], row["total"]),
        reverse=True,
    )

    indicators: list[dict[str, Any]] = []
    for title, count in indicator_counts.most_common(top_n):
        meta = indicator_meta[title]
        indicators.append(
            RiskIndicator(
                title=title,
                category=meta["category"],
                count=count,
                tasks=len(meta["tasks"]),
                max_severity=_max_severity(meta["severities"]),
                source_ref_count=meta["source_refs"],
            ).as_dict()
        )

    return {
        "total_findings": total,
        "total_tasks": len(tasks),
        "isolation_forest_signals": isolation_forest_total,
        "human_review_required": human_review_total,
        "findings_with_sources": findings_with_sources,
        "by_category": _ordered_counter(by_category, CATEGORY_ORDER),
        "by_severity": _ordered_counter(by_severity, SEVERITY_ORDER),
        "category_severity": {
            category: _ordered_counter(sev, SEVERITY_ORDER)
            for category, sev in category_severity.items()
        },
        "tasks": task_rollup,
        "top_indicators": indicators,
    }


def collect_records(db: Any) -> list[dict]:
    """Read every stored finding across matters, annotated with Task identity.

    Read-only: iterates ``db.list_matters()`` and ``db.get_findings(matter_id)``
    without mutating any state or collecting new data.
    """
    records: list[dict] = []
    for matter in db.list_matters():
        matter_id = matter["id"]
        matter_name = matter.get("name") or matter_id
        for finding in db.get_findings(matter_id):
            record = dict(finding)
            record["matter_id"] = matter_id
            record["matter_name"] = matter_name
            records.append(record)
    return records
