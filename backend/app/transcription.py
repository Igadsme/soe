"""Optional Faster-Whisper boundary. Installation is intentionally optional for the CPU-only base image."""
from pathlib import Path

from .config import settings
from .pipeline import TranscriptSegment, Word


class TranscriptionUnavailable(RuntimeError):
    pass


class FasterWhisperTranscriber:
    def __init__(self, model_size: str = "small"):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriptionUnavailable("faster-whisper is not installed; enable the inference worker extra") from exc
        self.model = WhisperModel(model_size, device="auto", compute_type="int8", download_root=settings.whisper_cache_dir)

    def transcribe(self, media_path: Path) -> list[TranscriptSegment]:
        segments, _ = self.model.transcribe(str(media_path), word_timestamps=True)
        return [TranscriptSegment(float(item.start), float(item.end), item.text.strip(),
                                  tuple(Word(float(w.start), float(w.end), w.word.strip()) for w in (getattr(item, "words", None) or []) if w.word.strip()))
                for item in segments if item.text.strip()]
