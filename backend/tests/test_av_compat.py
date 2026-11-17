"""Regression: faster-whisper decodes file paths with PyAV using ``metadata_errors``.

PyAV 19 removed that keyword, which made every background transcription job fail
(found by the local production-compose E2E). Keep the pinned PyAV compatible.
"""
import pytest

av = pytest.importorskip("av")


def test_pyav_accepts_faster_whisper_open_arguments(tmp_path):
    with pytest.raises((FileNotFoundError, av.error.FileNotFoundError, OSError)):
        av.open(str(tmp_path / "missing.mp4"), mode="r", metadata_errors="ignore")
