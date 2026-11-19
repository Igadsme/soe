"""Cheap whole-source signals computed with FFmpeg + numpy (CPU only, bounded by timeouts and frame budgets).

All classifications here are documented heuristics, not trained models:
- audio windows are labeled silence / speech / tonal / burst from RMS energy, zero-crossing rate and
  spectral flatness of a 16 kHz mono downmix;
- motion is mean absolute frame difference on low-resolution grayscale frames;
- "speaker turns" are pause/energy-change boundaries between transcript segments, not diarization.
"""
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .config import settings
from .media import MediaProcessingError, _binary, run_media_command

SAMPLE_RATE = 16000
SCENE_PTS = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


def run_binary(argv: list[str], timeout: float, limit_bytes: int) -> bytes:
    """Run FFmpeg writing raw data to stdout; kill on timeout or when output exceeds the byte budget."""
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)  # noqa: S603
    try:
        data, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise MediaProcessingError("signal extraction exceeded the configured deadline") from None
    if process.returncode:
        raise MediaProcessingError("signal extraction command failed")
    return data[:limit_bytes]


@dataclass
class AudioProfile:
    hop: float
    loudness_db: list[float]
    labels: list[str]
    peaks: list[float]
    silences: list[tuple[float, float]]

    def window(self, start: float, end: float) -> slice:
        return slice(max(0, int(start / self.hop)), max(0, int(end / self.hop)))


def audio_profile(path: Path, hop: float = 0.5, max_seconds: float | None = None) -> AudioProfile | None:
    max_seconds = max_seconds or settings.max_source_seconds
    data = run_binary([_binary("ffmpeg"), "-v", "error", "-nostdin", "-t", str(max_seconds), "-i", str(path), "-map", "0:a:0?",
                       "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"], settings.ffmpeg_timeout_seconds,
                      int(max_seconds * SAMPLE_RATE * 2) + 4096)
    samples = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    size = int(hop * SAMPLE_RATE)
    if samples.size < size:
        return None
    frames = samples[: samples.size // size * size].reshape(-1, size)
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-9)
    zcr = np.mean(np.abs(np.diff(np.signbit(frames).astype(np.int8), axis=1)), axis=1)
    spectrum = np.abs(np.fft.rfft(frames * np.hanning(size), axis=1)) + 1e-9
    flatness = np.exp(np.mean(np.log(spectrum), axis=1)) / np.mean(spectrum, axis=1)
    # Syllabic modulation: speech energy fluctuates strongly across 50 ms sub-frames; tones and noise are steady.
    sub = frames.reshape(frames.shape[0], -1, int(0.05 * SAMPLE_RATE)) if size % int(0.05 * SAMPLE_RATE) == 0 else frames[:, None, :]
    sub_rms = np.sqrt(np.mean(sub ** 2, axis=2) + 1e-12)
    modulation = np.std(sub_rms, axis=1) / (np.mean(sub_rms, axis=1) + 1e-9)
    silence_floor = max(-45.0, float(np.median(db)) - 18)
    labels = []
    for level, crossing, flat, mod in zip(db, zcr, flatness, modulation, strict=True):
        if level < silence_floor:
            labels.append("silence")
        elif flat < 0.02 and crossing < 0.2 and mod < 0.3:
            labels.append("tonal")  # stable narrowband: music notes, tones
        elif flat > 0.3 and mod < 0.35:
            labels.append("burst")  # steady broadband energy: crowd, impacts, effects
        else:
            labels.append("speech")
    median = float(np.median(db))
    peaks = []
    for index in range(1, len(db)):
        baseline = float(np.median(db[max(0, index - 8):index]))
        if db[index] > -30 and db[index] - baseline >= 10 and db[index] - median >= 8:
            if not peaks or index * hop - peaks[-1] > 2:
                peaks.append(round(index * hop, 2))
    silences, start = [], None
    for index, label in enumerate(labels + ["end"]):
        if label == "silence" and start is None:
            start = index
        elif label != "silence" and start is not None:
            if (index - start) * hop >= 0.5:
                silences.append((round(start * hop, 2), round(index * hop, 2)))
            start = None
    return AudioProfile(hop, [round(float(v), 2) for v in db], labels, peaks, silences)


def scene_changes(path: Path, threshold: float = 0.3) -> list[float]:
    result = run_media_command([_binary("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(path), "-an", "-vf",
                                f"scale=320:-2,select=gt(scene\\,{threshold}),showinfo", "-f", "null", "-"],
                               settings.ffmpeg_timeout_seconds)
    return [round(float(value), 2) for value in SCENE_PTS.findall(result.stderr)]


@dataclass
class MotionProfile:
    fps: float
    energy: list[float]
    column_energy: list[list[float]] = field(repr=False)
    width: int = 160

    def mean(self, start: float, end: float) -> float:
        values = self.energy[int(start * self.fps):max(int(start * self.fps) + 1, int(end * self.fps))]
        return float(np.mean(values)) if values else 0.0

    def action_center(self, start: float, end: float) -> float:
        """Horizontal centre (0-1) of moving content; static HUD/overlays contribute little energy."""
        rows = self.column_energy[int(start * self.fps):max(int(start * self.fps) + 1, int(end * self.fps))]
        if not rows:
            return 0.5
        columns = np.sum(np.array(rows), axis=0)
        total = float(columns.sum())
        if total <= 1e-6:
            return 0.5
        return float(np.dot(columns, np.arange(columns.size)) / total / max(1, columns.size - 1))


def motion_profile(path: Path, duration: float, fps: float | None = None, width: int = 160) -> MotionProfile | None:
    fps = fps or settings.signal_sample_fps
    # Frame budget: never sample more than signal_max_frames_per_hour (pro-rated), whatever the source length.
    budget = max(10, int(settings.signal_max_frames_per_hour * max(duration, 1) / 3600))
    fps = min(fps, budget / max(duration, 1))
    height = 90
    data = run_binary([_binary("ffmpeg"), "-v", "error", "-nostdin", "-i", str(path), "-an", "-vf",
                       f"fps={fps:.4f},scale={width}:{height},format=gray", "-frames:v", str(budget), "-f", "rawvideo", "-"],
                      settings.ffmpeg_timeout_seconds, budget * width * height)
    count = len(data) // (width * height)
    if count < 2:
        return None
    frames = np.frombuffer(data[: count * width * height], dtype=np.uint8).reshape(count, height, width).astype(np.int16)
    diff = np.abs(np.diff(frames, axis=0)).astype(np.float32) / 255.0
    energy = [0.0, *[round(float(v), 4) for v in diff.mean(axis=(1, 2))]]
    columns = [[0.0] * width, *[[round(float(v), 4) for v in row] for row in diff.mean(axis=1)]]
    return MotionProfile(fps, energy, columns, width)


def speaker_turns(segments, profile: AudioProfile | None, pause: float = 0.6, energy_jump_db: float = 6.0) -> list[float]:
    """Heuristic turn boundaries: long pauses or loudness level changes between consecutive segments."""
    turns = []
    for previous, current in zip(segments, segments[1:], strict=False):
        gap = current.start - previous.end
        jump = 0.0
        if profile:
            before = profile.loudness_db[profile.window(previous.start, previous.end)]
            after = profile.loudness_db[profile.window(current.start, current.end)]
            if before and after:
                jump = abs(float(np.median(after)) - float(np.median(before)))
        if gap >= pause or jump >= energy_jump_db:
            turns.append(round(current.start, 2))
    return turns


@dataclass
class SourceSignals:
    duration: float
    audio: AudioProfile | None
    scenes: list[float]
    motion: MotionProfile | None
    frames_sampled: int

    def summary(self) -> dict:
        return {"duration": self.duration, "scene_changes": self.scenes, "audio_peaks": self.audio.peaks if self.audio else [],
                "silences": self.audio.silences if self.audio else [], "frames_sampled": self.frames_sampled,
                "audio_labels": {label: (self.audio.labels.count(label) if self.audio else 0) for label in ("silence", "speech", "tonal", "burst")}}


def analyze(path: Path, duration: float, has_audio: bool) -> SourceSignals:
    """Stage 1 (cheap, whole source): audio curve, scene cuts, sampled motion."""
    audio = audio_profile(path, max_seconds=duration + 1) if has_audio else None
    motion = motion_profile(path, duration)
    return SourceSignals(duration, audio, scene_changes(path), motion, len(motion.energy) if motion else 0)


__all__ = ["AudioProfile", "MotionProfile", "SourceSignals", "analyze", "asdict", "audio_profile", "motion_profile",
           "scene_changes", "speaker_turns"]
