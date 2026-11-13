"""Run the local authorized-media E2E pipeline and emit playable MP4 artifacts."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.media import ingest_authorized, probe, render_vertical
from app.pipeline import TranscriptSegment, extract_candidates
from app.transcription import FasterWhisperTranscriber


def srt_timestamp(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def write_srt(path: Path, segments: list[TranscriptSegment], start: float, end: float) -> None:
    selected = [s for s in segments if s.end > start and s.start < end]
    with path.open("w", encoding="utf-8") as stream:
        for index, segment in enumerate(selected, 1):
            stream.write(f"{index}\n{srt_timestamp(max(0, segment.start - start))} --> {srt_timestamp(min(end - start, segment.end - start))}\n{segment.text}\n\n")


def main() -> None:
    sources = sorted(path for path in (ROOT / "outputs" / "e2e").glob("authorized_test_source*.mp4") if "_ingested" not in path.stem and "_clip_" not in path.stem)
    if len(sources) < 3:
        subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_test_media.py")], check=True)
        sources = sorted(path for path in (ROOT / "outputs" / "e2e").glob("authorized_test_source*.mp4") if "_ingested" not in path.stem and "_clip_" not in path.stem)
    transcriber = FasterWhisperTranscriber(model_size="tiny.en")
    reports = []
    for source in sources:
        stem = source.stem
        ingested, digest, metadata = ingest_authorized(str(source), ROOT / "outputs" / "e2e" / f"{stem}_ingested.mp4")
        segments = transcriber.transcribe(ingested)
        candidates = extract_candidates(segments, limit=3)
        if not candidates:
            raise RuntimeError(f"real transcription produced no timestamped candidate for {source}")
        outputs = []
        for index, candidate in enumerate(candidates, 1):
            subtitle = ROOT / "outputs" / "e2e" / f"{stem}_clip_{index}.srt"
            output = ROOT / "outputs" / "e2e" / f"{stem}_clip_{index}.mp4"
            write_srt(subtitle, segments, candidate.start, candidate.end)
            render_vertical(ingested, output, candidate.start, candidate.end, subtitle)
            rendered = probe(output)
            if rendered.width != 1080 or rendered.height != 1920 or rendered.duration <= 0:
                raise RuntimeError(f"invalid render: {output} {rendered}")
            outputs.append(str(output))
        reports.append({"source": str(source), "sha256": digest, "source_probe": metadata, "transcript_segments": len(segments), "clips": outputs})
    print({"videos": len(reports), "reports": reports})


if __name__ == "__main__":
    main()
