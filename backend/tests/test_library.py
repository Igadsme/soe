"""Library operations: review, real single-clip re-render through the killable media child, deletion, previews."""
import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main, worker
from app.config import settings
from app.models import AuditEvent, Clip, Job, Publication, Source

RIGHTS = {"authorization_type": "owned", "commercial_use": True, "publishing_allowed": True,
          "allowed_platforms": ["youtube"], "evidence": "Operator-owned original test recording"}
needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg missing")


def make_source(isolated_database, real=False):
    media = Path(settings.media_root) / "library.mp4"
    media.parent.mkdir(parents=True, exist_ok=True)
    if real:
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=12",  # noqa: S607
                        "-f", "lavfi", "-i", "sine=f=330:d=12", "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(media)], check=True, timeout=120)
    else:
        media.write_bytes(b"media")
    with TestClient(main.app) as client:
        source_id = client.post("/api/v1/sources", json={"platform": "upload", "external_id": "lib", "title": "Library",
                                                         "url": "https://example.test/lib", "media_path": str(media)}).json()["id"]
        assert client.post(f"/api/v1/sources/{source_id}/rights", json=RIGHTS).status_code == 201
    output = Path(settings.media_root) / "clips" / f"source-{source_id}" / "run-1" / "w" / "clip-1.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"old-render")
    subtitle = output.with_suffix(".srt")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nOld\n")
    with isolated_database() as db:
        clip = Clip(source_id=source_id, start_seconds=0, end_seconds=5, description="An original line of speech",
                    hook="Hook", score=80, status="pending_review", evidence={}, output_path=str(output),
                    subtitle_path=str(subtitle))
        db.add(clip)
        db.commit()
        return source_id, clip.id, output


def test_reject_and_delete_clip(isolated_database):
    _, clip_id, output = make_source(isolated_database)
    with TestClient(main.app) as client:
        assert client.post(f"/api/v1/clips/{clip_id}/reject").json()["status"] == "rejected"
        assert client.delete(f"/api/v1/clips/{clip_id}").status_code == 204
        assert client.delete(f"/api/v1/clips/{clip_id}").status_code == 404
    assert not output.exists()
    with isolated_database() as db:
        assert {"clip.reject", "clip.delete"} <= set(db.scalars(select(AuditEvent.action)))


def test_published_clip_cannot_be_deleted_or_rerendered(isolated_database):
    source_id, clip_id, output = make_source(isolated_database)
    with isolated_database() as db:
        db.add(Publication(clip_id=clip_id, platform="youtube", status="succeeded", idempotency_key="k" * 64))
        db.commit()
    with TestClient(main.app) as client:
        assert client.delete(f"/api/v1/clips/{clip_id}").status_code == 409
        assert client.post(f"/api/v1/clips/{clip_id}/rerender", json={"start_seconds": 1, "end_seconds": 6}).status_code == 409
        assert client.post(f"/api/v1/sources/{source_id}/archive").status_code == 409
    assert output.exists()


def test_rerender_validation(isolated_database):
    _, clip_id, _ = make_source(isolated_database)
    with TestClient(main.app) as client:
        for body in ({"start_seconds": 5, "end_seconds": 5}, {"start_seconds": 0, "end_seconds": 100000},
                     {"start_seconds": 0, "end_seconds": 5, "template": "x"}):
            assert client.post(f"/api/v1/clips/{clip_id}/rerender", json=body).status_code == 422


def test_archive_source_removes_media(isolated_database):
    source_id, _, output = make_source(isolated_database)
    with TestClient(main.app) as client:
        response = client.post(f"/api/v1/sources/{source_id}/archive")
        assert response.status_code == 200 and response.json()["clips_removed"] == 1
    assert not output.exists()
    with isolated_database() as db:
        source = db.get(Source, source_id)
        assert source.status == "archived" and source.media_path is None


def test_preview_supports_inline_range_requests(isolated_database):
    _, clip_id, _ = make_source(isolated_database)
    with TestClient(main.app) as client:
        response = client.get(f"/api/v1/clips/{clip_id}/download?inline=true", headers={"Range": "bytes=0-2"})
        assert response.status_code == 206 and response.content == b"old"
        assert response.headers["content-disposition"].startswith("inline")
        assert client.get(f"/api/v1/clips/{clip_id}/download").headers["content-disposition"].startswith("attachment")


@needs_ffmpeg
@pytest.mark.asyncio
async def test_real_rerender_then_quality_check(isolated_database, monkeypatch):
    monkeypatch.setattr(worker.Path, "touch", lambda self: None)
    monkeypatch.setattr(settings, "qc_min_clip_seconds", 1)
    monkeypatch.setattr(settings, "qc_min_mean_db", -60)
    _, clip_id, old = make_source(isolated_database, real=True)
    with TestClient(main.app) as client:
        response = client.post(f"/api/v1/clips/{clip_id}/rerender", json={"start_seconds": 2, "end_seconds": 6})
        assert response.status_code == 202
        assert client.delete(f"/api/v1/clips/{clip_id}").status_code == 409
    for _ in range(4):
        if not await worker.process_once():
            break
    with isolated_database() as db:
        clip = db.get(Clip, clip_id)
        jobs = {job.kind: job.status for job in db.scalars(select(Job))}
        assert jobs == {"render_clip": "succeeded", "quality_check": "succeeded"}, jobs
        assert (clip.start_seconds, clip.end_seconds) == (2, 6)
        assert clip.status == "pending_review", clip.evidence
        assert clip.evidence["qc_reasons"] == []
        assert Path(clip.output_path).stat().st_size > 1000 and not old.exists()
    await asyncio.sleep(0)
