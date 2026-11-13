"""Transparent ranking used until enough labeled outcomes exist for a learned model."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateSignals:
    hook_strength: float
    relevance: float
    emotional_engagement: float
    clarity: float
    novelty: float
    pacing: float
    payoff: float


def score(signals: CandidateSignals) -> float:
    weights = (0.20, 0.15, 0.15, 0.15, 0.10, 0.10, 0.15)
    values = tuple(max(0.0, min(1.0, v)) for v in signals.__dict__.values())
    return round(sum(v * w for v, w in zip(values, weights)) * 100, 2)
