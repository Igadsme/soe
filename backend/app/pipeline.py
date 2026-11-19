"""Evidence-first highlight extraction.

v1 (`extract_candidates`) ranks single transcript segments by punctuation only; it is kept as the evaluation baseline.
v2 (`extract_candidates_v2`) builds multi-sentence and event windows, snaps boundaries to pauses, scores hook,
coherence, payoff, audio excitement, scene density, pacing and speech coverage, and removes duplicates.
Every score component is stored in the candidate's evidence. Scores rank candidates; they do not predict virality.
"""
import re
from dataclasses import dataclass, field

from .ranking import CandidateSignals, score


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str
    words: tuple[Word, ...] = field(default=(), compare=False)


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


# ----------------------------------------------------------------------------------------------- v2
HOOK_WORDS = {"wait", "never", "secret", "mistake", "best", "worst", "why", "how", "incredible", "huge", "insane", "stop",
              "watch", "look", "finally", "first", "biggest", "shocking", "amazing", "what"}
PAYOFF_WORDS = ("here is", "here's", "the key", "lesson", "because", "turns out", "the result", "that's why", "so that",
                "which means", "the answer", "the trick", "remember")
CONTINUATION = {"and", "but", "so", "or", "because", "which", "then", "also"}
TOKEN = re.compile(r"[a-z0-9']+")


def tokens(text: str) -> set[str]:
    return set(TOKEN.findall(text.lower()))


def jaccard(a: str, b: str) -> float:
    left, right = tokens(a), tokens(b)
    return len(left & right) / len(left | right) if left and right else 0.0


def iou(a: tuple[float, float], b: tuple[float, float]) -> float:
    overlap = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return overlap / union if union > 0 else 0.0


def hook_strength(text: str) -> float:
    first = text.strip()[:120]
    lowered = first.lower()
    value = 0.25
    value += 0.25 if "?" in first[:80] else 0
    value += 0.2 if "!" in first[:80] else 0
    value += 0.15 if re.search(r"\b\d+\b|\b(one|two|three|five|ten)\b", lowered) else 0
    value += min(0.3, 0.1 * len(tokens(first[:60]) & HOOK_WORDS))
    return min(1.0, value)


def coherence(texts: list[str]) -> float:
    joined = " ".join(texts).strip()
    if not joined:
        return 0.0
    value = 0.4
    first_word = (TOKEN.findall(joined.lower()) or [""])[0]
    value += 0.2 if first_word not in CONTINUATION and joined[:1].isupper() else 0
    value += 0.2 if joined.rstrip()[-1:] in ".?!" else 0
    if any("?" in text for text in texts[:-1]):
        value += 0.2  # question followed by an answer inside the window
    return min(1.0, value)


def payoff(texts: list[str]) -> float:
    last = texts[-1].lower() if texts else ""
    joined = " ".join(texts).lower()
    value = 0.3 + (0.3 if "!" in last else 0) + (0.4 if any(word in joined for word in PAYOFF_WORDS) else 0)
    return min(1.0, value)


def _ratio(labels: list[str], wanted: set[str]) -> float:
    return sum(label in wanted for label in labels) / len(labels) if labels else 0.0


def window_signals(sig, start: float, end: float) -> dict:
    duration = max(end - start, 1e-6)
    result = {"audio_peaks": 0, "burst_ratio": 0.0, "speech_ratio": 0.0, "loudness_range_db": 0.0, "scene_cuts": 0, "motion": 0.0}
    if sig is None:
        return result
    if sig.audio:
        window = sig.audio.window(start, end)
        labels, levels = sig.audio.labels[window], sig.audio.loudness_db[window]
        result.update(audio_peaks=sum(start <= peak < end for peak in sig.audio.peaks), burst_ratio=round(_ratio(labels, {"burst"}), 3),
                      speech_ratio=round(_ratio(labels, {"speech"}), 3),
                      loudness_range_db=round(max(levels) - min(levels), 2) if levels else 0.0)
    result["scene_cuts"] = sum(start < cut < end for cut in sig.scenes)
    result["scene_cut_rate_per_10s"] = round(result["scene_cuts"] * 10 / duration, 3)
    if sig.motion:
        result["motion"] = round(sig.motion.mean(start, end), 4)
    return result


def _snap(sig, start: float, end: float, duration: float) -> tuple[float, float, str]:
    """Snap event windows to a preceding scene cut / silence end and a following silence start."""
    method = "event_window"
    if sig is not None:
        cuts = [cut for cut in sig.scenes if start - 1.5 <= cut <= start + 0.5]
        if cuts:
            start, method = cuts[-1], "snapped_to_scene_cut"
        if sig.audio:
            ends = [s for s, _ in sig.audio.silences if end - 1 <= s <= end + 2]
            if ends:
                end, method = ends[0], method + "+silence"
    return max(0.0, round(start, 2)), min(duration, round(end, 2)), method


def motion_spikes(sig) -> list[float]:
    if not sig or not sig.motion or len(sig.motion.energy) < 5:
        return []
    values = sig.motion.energy
    mean = sum(values) / len(values)
    spread = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
    spikes = []
    for index, value in enumerate(values):
        if spread > 0 and value > mean + 2 * spread and value > 0.02:
            time = index / sig.motion.fps
            if not spikes or time - spikes[-1] > 3:
                spikes.append(round(time, 2))
    return spikes


def extract_candidates_v2(segments: list[TranscriptSegment], sig=None, *, limit: int = 5, min_seconds: float = 4,
                          max_seconds: float = 45, duration: float | None = None) -> list[HighlightCandidate]:
    segments = [s for s in segments if s.end > s.start and s.text.strip()]
    duration = duration or (sig.duration if sig is not None else (segments[-1].end if segments else 0.0))
    windows: list[tuple[float, float, list[TranscriptSegment], str]] = []
    for i, first in enumerate(segments):
        for j in range(i, min(i + 4, len(segments))):
            group = segments[i:j + 1]
            raw = group[-1].end - first.start
            if raw > max_seconds:
                break
            if raw < min_seconds:
                continue
            before = first.start - (segments[i - 1].end if i else 0.0)
            after = (segments[j + 1].start if j + 1 < len(segments) else duration) - group[-1].end
            # Context-sensitive boundaries: small pre-roll into the pause before, let the payoff land after.
            start = max(0.0, first.start - min(0.25, max(0.0, before)))
            end = min(duration, group[-1].end + min(0.4, max(0.0, after)))
            windows.append((round(start, 2), round(end, 2), group, "sentence_aligned"))
    speech_spans = [(s.start, s.end) for s in segments]
    events = sorted(set((sig.audio.peaks if sig is not None and sig.audio else []) + motion_spikes(sig)))
    for peak in events:
        if any(a - 0.5 <= peak <= b + 0.5 for a, b in speech_spans):
            continue  # speech windows already cover it
        start, end, method = _snap(sig, peak - 3, peak + 5, duration)
        if end - start >= min_seconds:
            windows.append((start, end, [], method))

    candidates = []
    for start, end, group, method in windows:
        texts = [s.text.strip() for s in group]
        text = " ".join(texts)
        signals = window_signals(sig, start, end)
        length = end - start
        words = len(text.split())
        rate = words / length if length else 0.0
        hook = hook_strength(texts[0]) if texts else min(1.0, 0.3 + 0.2 * signals["audio_peaks"] + 3 * signals["motion"])
        clear = coherence(texts) if texts else 0.4
        pay = payoff(texts) if texts else min(1.0, 0.3 + 0.3 * signals["audio_peaks"])
        excitement = min(1.0, 0.2 + 0.25 * signals["audio_peaks"] + 0.5 * signals["burst_ratio"]
                         + signals["loudness_range_db"] / 60 + 0.1 * text.count("!"))
        novelty = min(1.0, 0.3 + signals.get("scene_cut_rate_per_10s", 0) / 3)
        pacing = (1.0 - min(1.0, abs(rate - 2.7) / 2.7)) if texts else min(1.0, 0.3 + 5 * signals["motion"])
        relevance = (signals["speech_ratio"] if sig is not None and sig.audio else 0.7) if texts else min(1.0, 0.4 + 5 * signals["motion"])
        components = CandidateSignals(hook, relevance, excitement, clear, novelty, pacing, pay)
        base = score(components)
        length_penalty = max(0.0, (length - 30) / max_seconds) * 10
        final = round(max(0.0, base - length_penalty), 2)
        description = text or f"Audio/visual event at {start:.1f}s"
        hook_text = (texts[0] if texts else description)[:90]
        evidence = {"version": "v2", "transcript": text, "source_start": start, "source_end": end, "boundary": method,
                    "components": {key: round(value, 3) for key, value in components.__dict__.items()},
                    "length_penalty": round(length_penalty, 2), "words_per_second": round(rate, 2), "signals": signals,
                    "segments": len(group)}
        candidates.append(HighlightCandidate(start, end, description, hook_text, "speech" if texts else "event", final, evidence))
    candidates.sort(key=lambda item: item.score, reverse=True)
    return dedupe(candidates, limit)


def dedupe(candidates: list[HighlightCandidate], limit: int, max_iou: float = 0.3, max_jaccard: float = 0.6) -> list[HighlightCandidate]:
    """Keep the best-scoring candidate among temporal overlaps or near-identical transcripts (repeated content)."""
    selected: list[HighlightCandidate] = []
    for candidate in candidates:
        span = (candidate.start, candidate.end)
        overlapping = any(iou(span, (prior.start, prior.end)) > max_iou or (min(span[1], prior.end) - max(span[0], prior.start)) > 0
                          for prior in selected)
        duplicate_text = candidate.evidence.get("transcript") and any(
            jaccard(candidate.evidence["transcript"], prior.evidence.get("transcript", "")) > max_jaccard for prior in selected)
        if overlapping or duplicate_text:
            continue
        selected.append(candidate)
        if len(selected) == limit:
            break
    return selected
