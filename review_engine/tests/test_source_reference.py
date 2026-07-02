from review_engine.extraction.models import source_reference


def test_source_reference_is_stable_and_location_specific():
    first = source_reference("M1", "a.pdf", page=2, ordinal=0)
    again = source_reference("M1", "a.pdf", page=2, ordinal=0)
    other = source_reference("M1", "a.pdf", page=3, ordinal=0)
    assert first == again
    assert first != other
    assert first.startswith("SRC-")
