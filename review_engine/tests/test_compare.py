"""Tests for the deterministic document compare / redline (RAYAAAA-231 / P1b)."""
from __future__ import annotations

from review_engine.compare.redline import (
    ComparisonResult,
    compare_documents,
    deterministic_summary,
    summarize_comparison,
)
from review_engine.extraction.models import SourceChunk, source_reference


def _chunk(document: str, text: str, ordinal: int, section: str = "Body") -> SourceChunk:
    ref = source_reference("MAT-TEST", document, section=section, ordinal=ordinal)
    return SourceChunk(
        matter_id="MAT-TEST",
        document_name=document,
        file_type="txt",
        text=text,
        source_ref=ref,
        section=section,
    )


class _FakeConnector:
    """Stand-in for OllamaConnector so the summary path stays local + offline."""

    def __init__(self, available: bool = True, output: str = "Fake summary. Requires human review."):
        self._available = available
        self._output = output
        self.prompts: list[str] = []

    def available(self) -> bool:
        return self._available

    def generate(self, prompt: str, timeout: int = 120) -> str:
        self.prompts.append(prompt)
        return self._output


def test_identical_documents_have_no_changes():
    base = [_chunk("v1.txt", "The term is 12 months.", 0)]
    comp = [_chunk("v2.txt", "The term is 12 months.", 0)]
    result = compare_documents("v1.txt", base, "v2.txt", comp)
    assert isinstance(result, ComparisonResult)
    assert not result.has_changes
    assert result.changed_segments == ()
    assert result.counts == {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}


def test_pure_addition_is_flagged_added_with_compare_ref():
    base = [_chunk("v1.txt", "Clause 1. Payment is net 30.", 0)]
    comp = [
        _chunk("v2.txt", "Clause 1. Payment is net 30.", 0),
        _chunk("v2.txt", "Clause 2. Late fees apply after 45 days.", 1, section="Clause 2"),
    ]
    result = compare_documents("v1.txt", base, "v2.txt", comp)
    added = [s for s in result.segments if s.kind == "added"]
    assert len(added) == 1
    assert "Late fees apply" in added[0].compare_text
    assert added[0].compare_source_refs  # anchored to the new version's chunk
    assert added[0].base_source_refs == ()
    assert result.counts["added"] == 1


def test_pure_removal_is_flagged_removed_with_base_ref():
    base = [
        _chunk("v1.txt", "Clause 1. Payment is net 30.", 0),
        _chunk("v1.txt", "Clause 2. Exclusive jurisdiction is London.", 1, section="Clause 2"),
    ]
    comp = [_chunk("v2.txt", "Clause 1. Payment is net 30.", 0)]
    result = compare_documents("v1.txt", base, "v2.txt", comp)
    removed = [s for s in result.segments if s.kind == "removed"]
    assert len(removed) == 1
    assert "Exclusive jurisdiction" in removed[0].base_text
    assert removed[0].base_source_refs
    assert removed[0].compare_source_refs == ()
    assert result.counts["removed"] == 1


def test_changed_sentence_within_chunk_is_isolated():
    # Only the first sentence differs; the second is identical and must not
    # appear as a change.
    base = [_chunk("v1.txt", "The term is 12 months. Notice is 30 days.", 0)]
    comp = [_chunk("v2.txt", "The term is 24 months. Notice is 30 days.", 0)]
    result = compare_documents("v1.txt", base, "v2.txt", comp)
    changed = [s for s in result.segments if s.kind == "changed"]
    assert len(changed) == 1
    assert changed[0].base_text == "The term is 12 months."
    assert changed[0].compare_text == "The term is 24 months."
    assert changed[0].base_source_refs and changed[0].compare_source_refs
    assert result.counts == {"added": 0, "removed": 0, "changed": 1, "unchanged": 0}


def test_include_unchanged_toggles_equal_segments():
    base = [_chunk("v1.txt", "Alpha clause. Beta clause.", 0)]
    comp = [_chunk("v2.txt", "Alpha clause. Gamma clause.", 0)]
    without = compare_documents("v1.txt", base, "v2.txt", comp)
    assert all(s.kind != "unchanged" for s in without.segments)
    withunchanged = compare_documents("v1.txt", base, "v2.txt", comp, include_unchanged=True)
    kinds = {s.kind for s in withunchanged.segments}
    assert "unchanged" in kinds
    # The changed part is present either way.
    assert withunchanged.counts["changed"] == without.counts["changed"] == 1


def test_formatting_only_difference_is_not_a_change():
    # Extra whitespace / case should not register as a change because matching
    # is done on the normalized key.
    base = [_chunk("v1.txt", "The   Term is 12 Months.", 0)]
    comp = [_chunk("v2.txt", "the term is 12 months.", 0)]
    result = compare_documents("v1.txt", base, "v2.txt", comp)
    assert not result.has_changes


def test_result_is_deterministic():
    base = [_chunk("v1.txt", "One. Two. Three.", 0)]
    comp = [_chunk("v2.txt", "One. Two point five. Three.", 0)]
    first = compare_documents("v1.txt", base, "v2.txt", comp).to_dict()
    second = compare_documents("v1.txt", base, "v2.txt", comp).to_dict()
    assert first == second


def test_to_dict_round_trips_structure():
    base = [_chunk("v1.txt", "Keep this. Drop that.", 0)]
    comp = [_chunk("v2.txt", "Keep this. Add other.", 0)]
    payload = compare_documents("v1.txt", base, "v2.txt", comp).to_dict()
    assert payload["base_document"] == "v1.txt"
    assert payload["compare_document"] == "v2.txt"
    assert set(payload["counts"]) == {"added", "removed", "changed", "unchanged"}
    assert any(seg["kind"] == "changed" for seg in payload["segments"])


def test_summary_without_connector_is_deterministic():
    base = [_chunk("v1.txt", "The term is 12 months.", 0)]
    comp = [_chunk("v2.txt", "The term is 24 months.", 0)]
    result = compare_documents("v1.txt", base, "v2.txt", comp)
    summary = summarize_comparison(result)
    assert summary == deterministic_summary(result)
    assert "1 changed" in summary


def test_summary_falls_back_when_connector_unavailable():
    base = [_chunk("v1.txt", "The term is 12 months.", 0)]
    comp = [_chunk("v2.txt", "The term is 24 months.", 0)]
    result = compare_documents("v1.txt", base, "v2.txt", comp)
    connector = _FakeConnector(available=False)
    summary = summarize_comparison(result, connector)
    assert summary == deterministic_summary(result)
    assert connector.prompts == []  # never called the model


def test_summary_uses_local_model_when_available():
    base = [_chunk("v1.txt", "The term is 12 months.", 0)]
    comp = [_chunk("v2.txt", "The term is 24 months.", 0)]
    result = compare_documents("v1.txt", base, "v2.txt", comp)
    connector = _FakeConnector(available=True, output="Term changed 12->24 months. Requires human review.")
    summary = summarize_comparison(result, connector)
    assert summary == "Term changed 12->24 months. Requires human review."
    assert len(connector.prompts) == 1
    # The prompt is source-anchored and bounded to the diff only.
    assert "SRC-" in connector.prompts[0]
    assert "DIFF:" in connector.prompts[0]


def test_no_change_summary_skips_model():
    base = [_chunk("v1.txt", "Identical text.", 0)]
    comp = [_chunk("v2.txt", "Identical text.", 0)]
    result = compare_documents("v1.txt", base, "v2.txt", comp)
    connector = _FakeConnector(available=True)
    summary = summarize_comparison(result, connector)
    assert "No differences found" in summary
    assert connector.prompts == []
