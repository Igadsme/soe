from app.pipeline import TranscriptSegment, extract_candidates


def test_candidates_have_source_evidence_and_do_not_overlap():
    items = extract_candidates([
        TranscriptSegment(0, 4, "A calm introduction"),
        TranscriptSegment(5, 10, "That was incredible! What just happened?"),
        TranscriptSegment(8, 12, "Overlapping context should be filtered"),
    ])
    assert items
    assert items[0].evidence["source_start"] == items[0].start
    assert all(items[i].end <= items[i + 1].start or items[i].start >= items[i + 1].end for i in range(len(items)) for _ in [0] if i + 1 < len(items))
