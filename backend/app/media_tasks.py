"""Isolated media computation and QC. Children never write database state."""
import asyncio
import ctypes
import json
import multiprocessing
import os
import re
import signal
from dataclasses import asdict
from pathlib import Path

from .config import settings
from .media import MediaProcessingError, _binary, probe, run_media_command, sha256_file
from .subtitles import (
    build_cues,
    distribute,
    parse_srt,
    readability_violations,
    write_srt,
)

_MEDIA_SETTINGS = (
    "ffmpeg_timeout_seconds", "max_source_seconds", "max_upload_bytes", "max_clip_seconds",
    "whisper_cache_dir", "whisper_model_size", "qc_min_clip_seconds", "qc_min_mean_db", "loudness_target_lufs",
    "edit_template", "brand_text", "signal_sample_fps", "signal_max_frames_per_hour", "highlight_min_seconds",
    "highlight_target_max_seconds",
)
LOUDNESS_TOLERANCE_LU = 2.0
CUE = re.compile(r"(\d{2,}:\d{2}:\d{2},\d{3}) --> (\d{2,}:\d{2}:\d{2},\d{3})")


def quality_check(items: list[dict], source_has_audio: bool) -> list[dict]:
    return [check_clip(item, source_has_audio) for item in items]


def timestamp_ms(value: str) -> int:
    hours, minutes, seconds, milliseconds = map(int, re.split("[:,]", value))
    return (3600 * hours + 60 * minutes + seconds) * 1000 + milliseconds


def check_clip(item: dict, source_has_audio: bool) -> dict:
    output, subtitle = Path(item["output"]), Path(item["subtitle"])
    reasons = []
    item_metrics: dict = {}
    duration_ms = 0
    digest = None
    try:
        if not output.is_file() or output.stat().st_size == 0:
            reasons.append("missing_or_empty_video")
        else:
            metadata = probe(output, require_ffprobe=True)
            duration_ms = round(metadata.duration * 1000)
            if (metadata.width, metadata.height) != (1080, 1920):
                reasons.append("unexpected_dimensions")
            if not settings.qc_min_clip_seconds <= metadata.duration <= settings.max_clip_seconds:
                reasons.append("duration_out_of_range")
            if source_has_audio and not metadata.has_audio:
                reasons.append("missing_audio")
            metrics = analyze_output(output, metadata.has_audio)
            item_metrics.update(metrics)
            if metadata.has_audio and (metrics.get("mean_volume_db") is None or metrics["mean_volume_db"] < settings.qc_min_mean_db):
                reasons.append("audio_below_loudness_floor")
            if metrics["black_ratio"] > 0.5:
                reasons.append("mostly_black_video")
            silent = not metadata.has_audio or metrics["silence_ratio"] >= 0.8
            if metrics["freeze_ratio"] >= 0.95 and silent:
                reasons.append("frozen_and_silent")  # a still frame alone is legitimate (slides, podcasts)
            if metadata.has_audio and source_has_audio and metrics["silence_ratio"] >= 0.8:
                reasons.append("mostly_silent_audio")
            loudness = metrics.get("integrated_lufs")
            if metadata.has_audio and loudness is not None and loudness > -70 and metrics["silence_ratio"] < 0.8 \
                    and abs(loudness - settings.loudness_target_lufs) > LOUDNESS_TOLERANCE_LU:
                # Advisory: platforms re-normalize playback loudness, so this warns instead of blocking.
                item_metrics.setdefault("warnings", []).append("loudness_out_of_tolerance")
            digest = sha256_file(output)
    except (MediaProcessingError, OSError, ValueError, KeyError, OverflowError):
        reasons.append("media_probe_failed")

    try:
        blocks = re.split(r"\n\s*\n", subtitle.read_text(encoding="utf-8").strip())
        for block in blocks:
            lines = block.splitlines()
            times = CUE.fullmatch(lines[1]) if len(lines) >= 3 else None
            if not times or not lines[0].isdigit() or not " ".join(lines[2:]).strip():
                reasons.append("invalid_or_empty_subtitles")
                break
            start, end = map(timestamp_ms, times.groups())
            if not 0 <= start < end <= duration_ms:
                reasons.append("subtitle_cue_out_of_range")
                break
        else:
            problems = readability_violations(parse_srt(subtitle.read_text(encoding="utf-8")), duration_ms / 1000)
            item_metrics["subtitle_readability"] = problems
            if problems:
                reasons.append("subtitle_readability")
    except (OSError, UnicodeError):
        reasons.append("missing_or_unreadable_subtitles")
    return {"clip_id": item["clip_id"], "reasons": reasons, "sha256": digest, "metrics": item_metrics}


def _sum(pattern: str, text: str) -> float:
    return sum(float(value) for value in re.findall(pattern, text))


def analyze_output(output: Path, has_audio: bool) -> dict:
    """One FFmpeg pass: blackdetect + freezedetect on video; volumedetect, silencedetect and EBU R128 on audio."""
    argv = [_binary("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(output), "-map", "0:v:0",
            "-vf", "blackdetect=d=0.5:pix_th=0.10,freezedetect=n=0.003:d=1"]
    if has_audio:
        argv += ["-map", "0:a:0", "-af", "volumedetect,silencedetect=n=-50dB:d=0.5,ebur128=framelog=quiet"]
    result = run_media_command(argv + ["-f", "null", "-"], settings.ffmpeg_timeout_seconds)
    log = result.stderr
    duration = probe(output, require_ffprobe=True).duration or 1.0
    freeze = _sum(r"freeze_duration: ([\d.]+)", log)
    if "freeze_start" in log and "freeze_end" not in log.split("freeze_start")[-1]:
        last = float(re.findall(r"freeze_start: ([\d.]+)", log)[-1])
        freeze += duration - last  # freeze ran to the end of the clip
    silence = _sum(r"silence_duration: ([\d.]+)", log)
    if has_audio and "silence_start" in log and "silence_end" not in log.split("silence_start")[-1]:
        silence += duration - float(re.findall(r"silence_start: (-?[\d.]+)", log)[-1])
    volume = re.search(r"mean_volume:\s*(-?[\d.]+) dB", log)
    integrated = re.findall(r"I:\s+(-?[\d.]+) LUFS", log)
    return {"black_ratio": round(min(1.0, _sum(r"black_duration:([\d.]+)", log) / duration), 3),
            "freeze_ratio": round(min(1.0, freeze / duration), 3),
            "silence_ratio": round(min(1.0, silence / duration), 3) if has_audio else 1.0,
            "mean_volume_db": float(volume[1]) if volume else None,
            "integrated_lufs": float(integrated[-1]) if integrated else None}


def probe_source(media_path: str) -> dict:
    source = Path(media_path)
    if not source.is_file() or source.stat().st_size > settings.max_upload_bytes:
        raise MediaProcessingError("source file is missing or exceeds the configured size limit")
    metadata = probe(source)
    if not 0 < metadata.duration <= settings.max_source_seconds:
        raise MediaProcessingError("source duration exceeds configured limit")
    return asdict(metadata)


def _child(target, args, result_file: Path, options: dict, parent_pid: int) -> None:
    # Linux worker containers. Own a process group so FFmpeg descendants die too.
    os.setsid()

    def parent_exit(_sig, _frame):
        os.killpg(os.getpgrp(), signal.SIGKILL)

    # SIGUSR1 on parent death distinguishes a crash from the supervised TERM grace.
    signal.signal(signal.SIGUSR1, parent_exit)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGUSR1, 0, 0, 0) != 0 or os.getppid() != parent_pid:
        parent_exit(None, None)
    for name, value in options.items():
        setattr(settings, name, value)
    try:
        value = target(*args)
        result_file.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")
    except BaseException:
        # Child exceptions can include file paths or provider/inference configuration.
        raise SystemExit(1) from None


async def terminate_child(process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        if process.is_alive():
            process.terminate()
    expires = asyncio.get_running_loop().time() + settings.media_terminate_grace_seconds
    while process.is_alive() and asyncio.get_running_loop().time() < expires:
        await asyncio.sleep(0.02)
    # The group can survive the leader if a descendant ignored TERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        if process.is_alive():
            process.kill()
    await asyncio.to_thread(process.join)


async def run_media_task(target, args, result_file: Path):
    options = {name: getattr(settings, name) for name in _MEDIA_SETTINGS}
    process = multiprocessing.get_context("spawn").Process(
        target=_child, args=(target, args, result_file, options, os.getpid()),
    )
    process.start()
    try:
        while process.is_alive():
            await asyncio.sleep(0.02)
        if process.exitcode != 0:
            raise MediaProcessingError("media child failed")
        return json.loads(result_file.read_text(encoding="utf-8"))
    except BaseException:
        await terminate_child(process)
        raise
    finally:
        process.close()


def render_subtitle(path: Path, duration: float, text: str) -> None:
    """Readable cues for operator-edited text without word timings (times distributed by word length)."""
    words = distribute(0.0, max(duration - 0.05, 0.1), " ".join(text.split()))
    write_srt(path, build_cues(words, duration) if words else [])


def render_single(source: str, output: str, subtitle: str, start: float, end: float, text: str) -> dict:
    """Re-render one clip with operator-edited bounds (runs in the killable media child)."""
    from .media import render_vertical
    render_subtitle(Path(subtitle), end - start, text)
    render_vertical(Path(source), Path(output), start, end, Path(subtitle))
    metadata = probe(Path(output))
    return {"has_audio": metadata.has_audio}
