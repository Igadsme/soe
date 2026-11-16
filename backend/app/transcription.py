"""Optional Faster-Whisper boundary. Installation is intentionally optional for the CPU-only base image."""
from pathlib import Path
import subprocess
import numpy as np
from .pipeline import TranscriptSegment
from .media import _binary


class TranscriptionUnavailable(RuntimeError):
    pass


class FasterWhisperTranscriber:
    def __init__(self, model_size: str = "small"):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriptionUnavailable("faster-whisper is not installed; enable the inference worker extra") from exc
        self.model = WhisperModel(model_size, device="auto", compute_type="int8")

    def transcribe(self, media_path: Path) -> list[TranscriptSegment]:
        # Passing a waveform avoids PyAV metadata_errors incompatibilities across wheels.
        raw = subprocess.run([_binary("ffmpeg"), "-v", "error", "-i", str(media_path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"], capture_output=True, check=True).stdout
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(audio, word_timestamps=True)
        return [TranscriptSegment(float(item.start), float(item.end), item.text.strip()) for item in segments if item.text.strip()]
