"""Evidence-first highlight extraction and persistent workflow orchestration primitives."""
from dataclasses import dataclass
from .ranking import CandidateSignals, score


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class HighlightCandidate:
    start: float
    end: float
    description: str
    hook: str
    category: str
    score: float
    evidence: dict


def extract_candidates(segments: list[TranscriptSegment], limit: int = 5) -> list[HighlightCandidate]:
    candidates: list[HighlightCandidate] = []
    for segment in segments:
        words = len(segment.text.split())
        if words < 3 or segment.end <= segment.start:
            continue
        emphasis = min(1.0, (segment.text.count("!") + segment.text.count("?") + words / 40) / 2)
        ranked = score(CandidateSignals(emphasis, 0.7, emphasis, 0.8, 0.5, 0.7, 0.7))
        candidates.append(HighlightCandidate(segment.start, segment.end, segment.text, segment.text[:90], "speech", ranked, {"transcript": segment.text, "source_start": segment.start, "source_end": segment.end}))
    candidates.sort(key=lambda item: item.score, reverse=True)
    selected: list[HighlightCandidate] = []
    for candidate in candidates:
        if any(not (candidate.end <= prior.start or candidate.start >= prior.end) for prior in selected):
            continue
        selected.append(candidate)
        if len(selected) == limit:
            break
    return selected
