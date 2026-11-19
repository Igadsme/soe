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
    return round(sum(v * w for v, w in zip(values, weights, strict=True)) * 100, 2)


# ---------------------------------------------------------------------------
# Feedback loop: learned weights from verified performance, blended with the heuristic.
# ---------------------------------------------------------------------------
import math  # noqa: E402

FEATURES = ("heuristic_score", "duration_minutes", "hour_sin", "hour_cos")
BLEND_CAP = 0.5


@dataclass(frozen=True)
class Observation:
    heuristic_score: float  # 0-100
    duration_seconds: float
    posted_hour: int  # 0-23, account timezone not assumed: UTC
    views: float
    average_view_percentage: float | None = None


def feature_vector(heuristic_score: float, duration_seconds: float, posted_hour: int) -> list[float]:
    angle = 2 * math.pi * (posted_hour % 24) / 24
    return [heuristic_score / 100, duration_seconds / 60, math.sin(angle), math.cos(angle)]


def zscores(values: list[float]) -> list[float]:
    mean = sum(values) / len(values)
    spread = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values)) or 1.0
    return [(v - mean) / spread for v in values]


def targets(observations: list[Observation]) -> list[float]:
    """Normalized performance: z(log1p views), averaged with z(view percentage) when every row has it."""
    views = zscores([math.log1p(max(0.0, o.views)) for o in observations])
    if all(o.average_view_percentage is not None for o in observations):
        retention = zscores([float(o.average_view_percentage) for o in observations])
        return [(a + b) / 2 for a, b in zip(views, retention, strict=True)]
    return views


def fit_weights(observations: list[Observation], ridge: float = 1.0) -> dict[str, float]:
    """Ridge regression (closed form) on standardized targets; bias first."""
    import numpy as np

    x = np.array([[1.0, *feature_vector(o.heuristic_score, o.duration_seconds, o.posted_hour)] for o in observations])
    y = np.array(targets(observations))
    penalty = ridge * np.eye(x.shape[1])
    penalty[0, 0] = 0.0  # do not shrink the intercept
    weights = np.linalg.solve(x.T @ x + penalty, x.T @ y)
    return {"bias": float(weights[0]), **{name: float(w) for name, w in zip(FEATURES, weights[1:], strict=True)}}


def blend_weight(sample_size: int, minimum: int) -> float:
    if sample_size < minimum:
        return 0.0
    return round(min(BLEND_CAP, sample_size / (sample_size + 3 * minimum)), 3)


def blended_score(heuristic_score: float, duration_seconds: float, posted_hour: int, weights: dict[str, float] | None,
                  sample_size: int, minimum: int) -> float:
    """Heuristic score adjusted by the learned predictor; the predictor is mapped to 0-100 by a logistic."""
    share = blend_weight(sample_size, minimum)
    if not weights or share == 0:
        return round(heuristic_score, 2)
    features = feature_vector(heuristic_score, duration_seconds, posted_hour)
    prediction = weights["bias"] + sum(weights[name] * value for name, value in zip(FEATURES, features, strict=True))
    learned = 100 / (1 + math.exp(-prediction))
    return round((1 - share) * heuristic_score + share * learned, 2)


def recommendations(observations: list[Observation], minimum: int) -> dict:
    n = len(observations)
    base = {"sample_size": n, "required_samples": minimum, "target": "7-day views (log, standardized), averaged with "
            "average view percentage when available", "features": list(FEATURES),
            "disclaimer": "Correlational guidance from your own verified metrics; it does not predict virality."}
    if n < minimum:
        return {**base, "state": "insufficient_data", "weights": None, "blend_weight": 0.0}
    weights = fit_weights(observations)
    best_hour = max(range(24), key=lambda hour: weights["hour_sin"] * math.sin(2 * math.pi * hour / 24)
                    + weights["hour_cos"] * math.cos(2 * math.pi * hour / 24))
    return {**base, "state": "learned", "weights": weights, "blend_weight": blend_weight(n, minimum),
            "suggestions": {"best_posting_hour_utc": best_hour,
                            "duration": "shorter" if weights["duration_minutes"] < 0 else "longer",
                            "heuristic_agrees_with_outcomes": weights["heuristic_score"] > 0}}
