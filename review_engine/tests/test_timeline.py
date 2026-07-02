from review_engine.evidence.timeline import build_timeline
from review_engine.extraction.models import SourceChunk


def test_timeline_is_chronological_and_source_linked():
    chunks = [
        SourceChunk("M1", "case.txt", "txt", "Closed on 03/04/2025.", "SRC-B"),
        SourceChunk("M1", "case.txt", "txt", "Opened on January 2, 2025.", "SRC-A"),
    ]
    timeline = build_timeline(chunks)
    assert [item["source_ref"] for item in timeline] == ["SRC-A", "SRC-B"]
    assert all(item["citation"] for item in timeline)
