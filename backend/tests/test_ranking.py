from app.ranking import CandidateSignals, score


def test_ranking_is_bounded_and_deterministic():
    signals = CandidateSignals(1, 1, 1, 1, 1, 1, 1)
    assert score(signals) == 100
    assert score(CandidateSignals(0, 0, 0, 0, 0, 0, 0)) == 0
