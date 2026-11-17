"""Authorized local media handling and FFmpeg-backed render validation."""
import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import settings


class MediaProcessingError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaProbe:
    duration: float
    width: int
    height: int
    has_audio: bool


def detect_scene_boundaries(path: Path, threshold: float = 0.35) -> list[float]:
    """Detect scene boundaries using FFmpeg's scene score filter."""
    expression = f"select=gt(scene\\,{threshold}),showinfo"
    result = run_media_command([_binary("ffmpeg"), "-hide_banner", "-i", str(path), "-vf", expression, "-an", "-f", "null", "-"], settings.ffmpeg_timeout_seconds)
    return [float(match) for match in re.findall(r"pts_time:([0-9]+(?:\\.[0-9]+)?)", result.stderr)]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _binary(name: str) -> str:
    value = shutil.which(name)
    if not value and name == "ffmpeg":
        try:
            import imageio_ffmpeg
            value = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            value = None
    if not value:
        raise MediaProcessingError(f"{name} is required but not installed")
    return value


def run_media_command(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run a media command with a hard kill, rather than leaving descendants alive."""
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)  # noqa: S603
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise MediaProcessingError("media processing exceeded the configured deadline") from None
    if process.returncode:
        raise MediaProcessingError("media processing command failed")
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def probe(path: Path) -> MediaProbe:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        result = run_media_command([ffprobe, "-v", "error", "-show_entries", "format=duration", "-show_streams", "-of", "json", str(path)], settings.ffmpeg_timeout_seconds)
        body = json.loads(result.stdout)
        video = next((s for s in body.get("streams", []) if s.get("codec_type") == "video"), None)
        if not video:
            raise MediaProcessingError("media has no video stream")
        return MediaProbe(float(body["format"]["duration"]), int(video["width"]), int(video["height"]), any(s.get("codec_type") == "audio" for s in body.get("streams", [])))
    result = run_media_command([_binary("ffmpeg"), "-hide_banner", "-i", str(path), "-f", "null", "-"], settings.ffmpeg_timeout_seconds)
    text = result.stderr
    duration_match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    video_match = re.search(r"Video: .*?(\d{2,5})x(\d{2,5})", text)
    if not duration_match or not video_match:
        raise MediaProcessingError(f"could not probe media: {text[-500:]}")
    hours, minutes, seconds = duration_match.groups()
    return MediaProbe(int(hours) * 3600 + int(minutes) * 60 + float(seconds), int(video_match.group(1)), int(video_match.group(2)), "Audio:" in text or " audio" in text)


def ingest_authorized(source_path: str, destination: Path) -> tuple[Path, str, MediaProbe]:
    source = Path(source_path).expanduser().resolve()
    if not source.is_file() or source.stat().st_size > settings.max_upload_bytes:
        raise MediaProcessingError("source file is missing or exceeds the configured size limit")
    metadata = probe(source)
    if not 0 < metadata.duration <= settings.max_source_seconds:
        raise MediaProcessingError("source duration exceeds configured limit")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    digest = sha256_file(destination)
    return destination, digest, metadata


def render_vertical(source: Path, output: Path, start: float, end: float, subtitle_file: Path | None = None) -> Path:
    if end <= start or end - start > settings.max_clip_seconds:
        raise MediaProcessingError("clip duration is invalid")
    output.parent.mkdir(parents=True, exist_ok=True)
    vf = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1"
    if subtitle_file:
        vf += f",subtitles={subtitle_file.as_posix()}"
    run_media_command([_binary("ffmpeg"), "-y", "-ss", str(start), "-to", str(end), "-i", str(source), "-vf", vf, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", str(output)], settings.ffmpeg_timeout_seconds)
    if not output.is_file() or output.stat().st_size == 0:
        raise MediaProcessingError("FFmpeg did not produce a usable output")
    return output
