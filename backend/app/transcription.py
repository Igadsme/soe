"""Optional Faster-Whisper boundary. Installation is intentionally optional for the CPU-only base image."""
from pathlib import Path
from .pipeline import TranscriptSegment


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
        segments, _ = self.model.transcribe(str(media_path), word_timestamps=True)
        return [TranscriptSegment(float(item.start), float(item.end), item.text.strip()) for item in segments if item.text.strip()]
