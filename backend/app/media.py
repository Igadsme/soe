"""Authorized local media handling and FFmpeg-backed render validation."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import shutil
import subprocess
from .config import settings


class MediaProcessingError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaProbe:
    duration: float
    width: int
    height: int
    has_audio: bool


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _binary(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise MediaProcessingError(f"{name} is required but not installed")
    return value


def probe(path: Path) -> MediaProbe:
    result = subprocess.run([_binary("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-show_streams", "-of", "json", str(path)], capture_output=True, text=True, check=True)
    import json
    body = json.loads(result.stdout)
    video = next((s for s in body.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise MediaProcessingError("media has no video stream")
    return MediaProbe(float(body["format"]["duration"]), int(video["width"]), int(video["height"]), any(s.get("codec_type") == "audio" for s in body.get("streams", [])))


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
