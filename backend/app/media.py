"""Authorized local media handling and FFmpeg-backed render validation."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import shutil
import subprocess
import re
import json
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
    result = subprocess.run([_binary("ffmpeg"), "-hide_banner", "-i", str(path), "-vf", expression, "-an", "-f", "null", "-"], capture_output=True, text=True)
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


def probe(path: Path) -> MediaProbe:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        result = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-show_streams", "-of", "json", str(path)], capture_output=True, text=True, check=True)
        body = json.loads(result.stdout)
        video = next((s for s in body.get("streams", []) if s.get("codec_type") == "video"), None)
        if not video:
            raise MediaProcessingError("media has no video stream")
        return MediaProbe(float(body["format"]["duration"]), int(video["width"]), int(video["height"]), any(s.get("codec_type") == "audio" for s in body.get("streams", [])))
    result = subprocess.run([_binary("ffmpeg"), "-hide_banner", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)
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
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    digest = sha256_file(destination)
    return destination, digest, probe(destination)


def render_vertical(source: Path, output: Path, start: float, end: float, subtitle_file: Path | None = None) -> Path:
    if end <= start or end - start > settings.max_clip_seconds:
        raise MediaProcessingError("clip duration is invalid")
    output.parent.mkdir(parents=True, exist_ok=True)
    vf = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1"
    if subtitle_file:
        vf += f",subtitles={subtitle_file.as_posix()}"
    subprocess.run([_binary("ffmpeg"), "-y", "-ss", str(start), "-to", str(end), "-i", str(source), "-vf", vf, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", str(output)], capture_output=True, text=True, check=True)
    if not output.is_file() or output.stat().st_size == 0:
        raise MediaProcessingError("FFmpeg did not produce a usable output")
    return output
