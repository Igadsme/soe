"""Generate original, locally owned media for integration tests."""
from pathlib import Path
import subprocess
import sys

import imageio_ffmpeg


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "e2e"
OUT.mkdir(parents=True, exist_ok=True)
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def main() -> None:
    fixtures = [
        ("authorized_test_source.mp4", "This is an original authorized test video. The surprising moment is clearly marked for clipping.", "testsrc2=size=1280x720:rate=30", 6),
        ("authorized_test_source_long.mp4", "This is a longer original authorized recording. The key educational explanation happens in the middle.", "smptebars=size=1280x720:rate=30", 14),
        ("authorized_test_source_pattern.mp4", "This is an original visual pattern test. The high contrast scene change is intentional and authorized.", "color=c=navy:size=1280x720:rate=30", 8),
    ]
    for filename, text, pattern, duration in fixtures:
        audio = OUT / f"{Path(filename).stem}.aiff"
        video = OUT / filename
        subprocess.run(["say", text, "-o", str(audio)], check=True)
        subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i", pattern, "-stream_loop", "-1", "-i", str(audio), "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(video)


if __name__ == "__main__":
    main()
