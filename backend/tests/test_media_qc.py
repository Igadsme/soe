"""Automated output validation (QC) against real FFmpeg-rendered media, plus token key rotation."""
import shutil
import subprocess

import pytest
from cryptography.fernet import Fernet

from app import media_tasks, token_vault
from app.config import settings
from app.models import ConnectedAccount

ffmpeg = shutil.which("ffmpeg") and shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not ffmpeg, reason="ffmpeg/ffprobe not installed")


def render(path, size="1080x1920", duration=4, audio=True, volume=0.5):
    argv = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=25:d={duration}"]
    if audio:
        argv += ["-f", "lavfi", "-i", f"sine=f=440:d={duration},volume={volume}"]
    argv += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    argv += ["-c:a", "aac", "-shortest"] if audio else ["-an"]
    subprocess.run([*argv, str(path)], check=True, timeout=60)  # noqa: S603
    return path


def srt(path, text="1\n00:00:00,000 --> 00:00:02,000\nHello there\n"):
    path.write_text(text, encoding="utf-8")
    return path


@needs_ffmpeg
def test_qc_accepts_valid_vertical_clip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "qc_min_clip_seconds", 1)
    clip = render(tmp_path / "ok.mp4")
    result, = media_tasks.quality_check([{"clip_id": 1, "output": str(clip), "subtitle": str(srt(tmp_path / "ok.srt"))}], True)
    assert result["reasons"] == [] and len(result["sha256"]) == 64


@needs_ffmpeg
def test_qc_rejects_bad_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "qc_min_clip_seconds", 1)
    landscape = render(tmp_path / "landscape.mp4", size="640x360")
    silent = render(tmp_path / "silent.mp4", audio=False)
    quiet = render(tmp_path / "quiet.mp4", volume=0.0)
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    bad_srt = srt(tmp_path / "bad.srt", "1\n00:00:00,000 --> 00:09:00,000\nToo long\n")
    good_srt = srt(tmp_path / "good.srt")
    results = media_tasks.quality_check([
        {"clip_id": 1, "output": str(landscape), "subtitle": str(good_srt)},
        {"clip_id": 2, "output": str(silent), "subtitle": str(good_srt)},
        {"clip_id": 3, "output": str(quiet), "subtitle": str(good_srt)},
        {"clip_id": 4, "output": str(empty), "subtitle": str(tmp_path / "missing.srt")},
        {"clip_id": 5, "output": str(render(tmp_path / "ok.mp4")), "subtitle": str(bad_srt)},
    ], True)
    reasons = {item["clip_id"]: set(item["reasons"]) for item in results}
    assert "unexpected_dimensions" in reasons[1]
    assert "missing_audio" in reasons[2]
    assert "audio_below_loudness_floor" in reasons[3]
    assert {"missing_or_empty_video", "missing_or_unreadable_subtitles"} <= reasons[4]
    assert "subtitle_cue_out_of_range" in reasons[5]


@needs_ffmpeg
def test_probe_source_limits(tmp_path, monkeypatch):
    clip = render(tmp_path / "source.mp4", duration=3)
    assert media_tasks.probe_source(str(clip))["duration"] > 2
    monkeypatch.setattr(settings, "max_source_seconds", 1)
    with pytest.raises(media_tasks.MediaProcessingError):
        media_tasks.probe_source(str(clip))
    with pytest.raises(media_tasks.MediaProcessingError):
        media_tasks.probe_source(str(tmp_path / "missing.mp4"))


def test_token_key_rotation_reencrypts_with_primary(isolated_database, monkeypatch):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "token_encryption_keys", old)
    with isolated_database() as db:
        vault = token_vault.token_vault()
        db.add(ConnectedAccount(platform="youtube", external_account_id="rotate", display_name="Rotate",
                                access_token=vault.encrypt(b"access-value").decode(),
                                refresh_token=vault.encrypt(b"refresh-value").decode()))
        db.commit()
    monkeypatch.setattr(settings, "token_encryption_keys", f"{new},{old}")
    with isolated_database() as db:
        assert token_vault.rotate_tokens(db, token_vault.token_vault()) == 2
    monkeypatch.setattr(settings, "token_encryption_keys", new)
    with isolated_database() as db:
        account = db.query(ConnectedAccount).one()
        assert b"access-value" not in account.access_token.encode()
        assert token_vault.token_vault().decrypt(account.access_token.encode()) == b"access-value"
