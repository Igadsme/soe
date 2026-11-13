import os
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.skipif(os.getenv("SOE_RUN_MEDIA_E2E") != "1", reason="set SOE_RUN_MEDIA_E2E=1 for the real model/render integration test")
def test_authorized_media_pipeline_outputs_playable_vertical_clips():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, str(root / "scripts" / "run_e2e.py")], cwd=root, env={**os.environ, "PYTHONPATH": str(root / "backend")}, capture_output=True, text=True, check=True)
    assert "'videos': 3" in result.stdout
    assert len(list((root / "outputs" / "e2e").glob("*_clip_*.mp4"))) >= 3
