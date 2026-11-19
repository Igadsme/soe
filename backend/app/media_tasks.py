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
from .subtitles import srt_timestamp

_MEDIA_SETTINGS = (
    "ffmpeg_timeout_seconds", "max_source_seconds", "max_upload_bytes", "max_clip_seconds",
    "whisper_cache_dir", "whisper_model_size", "qc_min_clip_seconds", "qc_min_mean_db",
)
CUE = re.compile(r"(\d{2,}:\d{2}:\d{2},\d{3}) --> (\d{2,}:\d{2}:\d{2},\d{3})")


def quality_check(items: list[dict], source_has_audio: bool) -> list[dict]:
    return [check_clip(item, source_has_audio) for item in items]


def timestamp_ms(value: str) -> int:
    hours, minutes, seconds, milliseconds = map(int, re.split("[:,]", value))
    return (3600 * hours + 60 * minutes + seconds) * 1000 + milliseconds


def check_clip(item: dict, source_has_audio: bool) -> dict:
    output, subtitle = Path(item["output"]), Path(item["subtitle"])
    reasons = []
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
            if metadata.has_audio:
                result = run_media_command([_binary("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(output),
                    "-map", "0:a:0", "-af", "volumedetect", "-vn", "-f", "null", "-"], settings.ffmpeg_timeout_seconds)
                volume = re.search(r"mean_volume:\s*(-?[\d.]+) dB", result.stderr)
                if not volume or float(volume[1]) < settings.qc_min_mean_db:
                    reasons.append("audio_below_loudness_floor")
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
    except (OSError, UnicodeError):
        reasons.append("missing_or_unreadable_subtitles")
    return {"clip_id": item["clip_id"], "reasons": reasons, "sha256": digest}


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
    path.write_text(f"1\n00:00:00,000 --> {srt_timestamp(duration)}\n{' '.join(text.split())}\n", encoding="utf-8")
