"""Subtitle timing, readability rules and SRT/ASS writers.

Cue rules (broadcast-style defaults): at most 2 lines of 32 characters, 0.8-6.0 s on screen, at most 20 characters per
second, cues never overlap and never extend past the clip. Cues are built from Whisper word timestamps when present;
otherwise word times are distributed across the segment in proportion to word length (documented approximation).
"""
from dataclasses import dataclass

MAX_LINE_CHARS = 32
MAX_LINES = 2
MIN_SECONDS = 0.8
MAX_SECONDS = 6.0
MAX_CPS = 20.0


def srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, centiseconds = divmod(centiseconds, 360000)
    minutes, centiseconds = divmod(centiseconds, 6000)
    seconds, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02}:{seconds:02}.{centiseconds:02}"


@dataclass
class Cue:
    start: float
    end: float
    lines: list[str]

    @property
    def text(self) -> str:
        return " ".join(self.lines)


def wrap(words: list[str]) -> list[str] | None:
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= MAX_LINE_CHARS or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines if len(lines) <= MAX_LINES and all(len(line) <= MAX_LINE_CHARS for line in lines) else None


def distribute(start: float, end: float, text: str) -> list[tuple[float, float, str]]:
    words = text.split()
    if not words or end <= start:
        return []
    weights = [len(word) + 1 for word in words]
    total, cursor, timed = sum(weights), start, []
    for word, weight in zip(words, weights, strict=True):
        length = (end - start) * weight / total
        timed.append((cursor, cursor + length, word))
        cursor += length
    return timed


def timed_words(segments, clip_start: float, clip_end: float) -> list[tuple[float, float, str]]:
    """Clip-relative (start, end, word) for words fully inside [clip_start, clip_end]."""
    result = []
    for segment in segments:
        words = [(w.start, w.end, w.text.strip()) for w in getattr(segment, "words", ()) if w.text.strip()]
        words = words or distribute(segment.start, segment.end, segment.text)
        for start, end, word in words:
            if start >= clip_start - 0.05 and end <= clip_end + 0.05:
                result.append((max(0.0, start - clip_start), min(clip_end - clip_start, end - clip_start), word))
    return result


def build_cues(words: list[tuple[float, float, str]], duration: float) -> list[Cue]:
    cues: list[Cue] = []
    group: list[tuple[float, float, str]] = []

    def flush():
        if group:
            cues.append(Cue(group[0][0], group[-1][1], wrap([w for _, _, w in group]) or [" ".join(w for _, _, w in group)]))
            group.clear()

    for item in words:
        proposed = [*group, item]
        text = " ".join(w for _, _, w in proposed)
        too_long = wrap([w for _, _, w in proposed]) is None or item[1] - proposed[0][0] > MAX_SECONDS
        sentence_end = group and group[-1][2][-1:] in ".?!" and item[0] - group[0][0] >= MIN_SECONDS
        if group and (too_long or sentence_end or len(text) / max(item[1] - proposed[0][0], 1e-3) > MAX_CPS * 1.6):
            flush()
        group.append(item)
    flush()
    # Timing pass: enforce minimum display and reading speed using the gap before the next cue; never overlap.
    for index, cue in enumerate(cues):
        limit = cues[index + 1].start if index + 1 < len(cues) else duration
        needed = max(MIN_SECONDS, len(cue.text) / MAX_CPS)
        cue.end = round(min(limit, max(cue.end, cue.start + needed), cue.start + MAX_SECONDS), 3)
        cue.start = round(cue.start, 3)
    return [cue for cue in cues if cue.end > cue.start]


def readability_violations(cues: list[Cue], duration: float | None = None) -> list[str]:
    problems = []
    for index, cue in enumerate(cues, 1):
        length = cue.end - cue.start
        if len(cue.lines) > MAX_LINES or any(len(line) > MAX_LINE_CHARS + 8 for line in cue.lines):
            problems.append(f"cue {index}: too many characters per line")
        if length > MAX_SECONDS + 0.01:
            problems.append(f"cue {index}: on screen longer than {MAX_SECONDS}s")
        if length < 0.3:
            problems.append(f"cue {index}: flashes for under 0.3s")
        if len(cue.text) / max(length, 1e-3) > MAX_CPS * 1.5:
            problems.append(f"cue {index}: reading speed above {MAX_CPS * 1.5:.0f} cps")
        if index < len(cues) and cues[index].start < cue.end - 0.001:
            problems.append(f"cue {index}: overlaps the next cue")
        if duration is not None and cue.end > duration + 0.05:
            problems.append(f"cue {index}: ends after the clip")
    return problems


def write_srt(path, cues: list[Cue]) -> None:
    blocks = [f"{i}\n{srt_timestamp(c.start)} --> {srt_timestamp(c.end)}\n" + "\n".join(c.lines) for i, c in enumerate(cues, 1)]
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def parse_srt(text: str) -> list[Cue]:
    import re
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        match = re.fullmatch(r"(\d+):(\d{2}):(\d{2}),(\d{3}) --> (\d+):(\d{2}):(\d{2}),(\d{3})", lines[1]) if len(lines) >= 3 else None
        if not match:
            continue
        values = list(map(int, match.groups()))
        cues.append(Cue(values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000,
                        values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000, lines[2:]))
    return cues


def ass_escape(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def write_ass(path, cues: list[Cue], template, duration: float, brand: str = "") -> None:
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Caption,{template.font},{template.font_size},{template.primary},&H000000FF,&H00000000,&H64000000,"
        f"{-1 if template.bold else 0},0,0,0,100,100,0,0,1,{template.outline},1,2,80,80,{template.margin_v},1\n"
        "Style: Brand,DejaVu Sans,40,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,2,0,9,40,40,60,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for cue in cues:
        text = "\\N".join(ass_escape(line) for line in cue.lines)
        events.append(f"Dialogue: 0,{ass_timestamp(cue.start)},{ass_timestamp(cue.end)},Caption,,0,0,0,,{text}")
    if brand:
        events.append(f"Dialogue: 1,{ass_timestamp(0)},{ass_timestamp(duration)},Brand,,0,0,0,,{ass_escape(brand)}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
