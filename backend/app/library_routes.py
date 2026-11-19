"""Content library operations: clip review, single-clip re-render, and safe deletion."""
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import audit
from .auth import require_auth
from .automation import ACTIVE, lock_policy
from .config import settings
from .db import session
from .models import Clip, Job, Publication, Source
from .publications import latest_rights, lock_source
from .rights import can_ingest_and_publish
from .scheduler import enqueue

router = APIRouter(prefix="/api/v1")


class RerenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_seconds: float = Field(ge=0, le=86400)
    end_seconds: float = Field(gt=0, le=86400)

    @model_validator(mode="after")
    def check_bounds(self):
        length = self.end_seconds - self.start_seconds
        if length <= 0:
            raise ValueError("end_seconds must be greater than start_seconds")
        if length > settings.max_clip_seconds or length < settings.qc_min_clip_seconds:
            raise ValueError("clip length is outside the configured limits")
        return self


def _clip_or_404(db, clip_id: int) -> Clip:
    clip = db.get(Clip, clip_id)
    if not clip:
        raise HTTPException(404, "clip not found")
    return clip


def _published(db, clip_id: int) -> bool:
    return db.scalar(select(Publication.id).where(Publication.clip_id == clip_id).limit(1)) is not None


def _remove_media(path: str | None) -> None:
    """Remove a generated file only when it resolves inside SOE_MEDIA_ROOT/clips."""
    if not path:
        return
    root = (Path(settings.media_root).resolve() / "clips")
    target = Path(path).resolve()
    if target.is_relative_to(root) and target.is_file() and not target.is_symlink():
        target.unlink()


@router.post("/clips/{clip_id}/reject")
def reject_clip(clip_id: int = PathParam(gt=0, le=2147483647), db: Session = Depends(session), actor: str = Depends(require_auth)) -> dict:
    lock_policy(db)
    clip = _clip_or_404(db, clip_id)
    if _published(db, clip_id):
        raise HTTPException(409, "clip is referenced by a publication")
    if clip.status not in {"pending_review", "approved", "qc_failed", "rendered"}:
        raise HTTPException(409, "clip is not reviewable in its current state")
    clip.status = "rejected"
    audit(db, actor, "clip.reject", "clip", clip.id)
    db.commit()
    return {"clip_id": clip.id, "status": clip.status}


@router.post("/clips/{clip_id}/rerender", status_code=202)
def rerender_clip(payload: RerenderRequest, clip_id: int = PathParam(gt=0, le=2147483647), db: Session = Depends(session),
                  actor: str = Depends(require_auth)) -> dict:
    lock_policy(db)
    clip = _clip_or_404(db, clip_id)
    lock_source(db, clip.source_id)
    source = db.get(Source, clip.source_id)
    rights = latest_rights(db, clip.source_id)
    if not source or not rights or not can_ingest_and_publish(rights):
        raise HTTPException(403, "current rights do not authorize processing")
    if _published(db, clip_id):
        raise HTTPException(409, "clip is referenced by a publication")
    active = db.scalar(select(Job.id).where(Job.source_id == clip.source_id, Job.status.in_(ACTIVE),
                                            Job.kind.in_(("process_source", "render_clip", "quality_check"))).limit(1))
    if active:
        raise HTTPException(409, "source processing is in progress")
    job = enqueue(db, "render_clip", source_id=clip.source_id, payload={
        "clip_id": clip.id, "start_seconds": payload.start_seconds, "end_seconds": payload.end_seconds,
        "media_revision": source.media_revision})
    clip.status = "rendering"
    audit(db, actor, "clip.rerender", "clip", clip.id, start=payload.start_seconds, end=payload.end_seconds)
    db.commit()
    return {"job_id": job.id, "status": job.status, "clip_id": clip.id}


@router.delete("/clips/{clip_id}", status_code=204)
def delete_clip(clip_id: int = PathParam(gt=0, le=2147483647), db: Session = Depends(session), actor: str = Depends(require_auth)) -> None:
    lock_policy(db)
    clip = _clip_or_404(db, clip_id)
    if _published(db, clip_id):
        raise HTTPException(409, "clip is referenced by a publication")
    if clip.status == "rendering":
        raise HTTPException(409, "clip is being re-rendered")
    _remove_media(clip.output_path)
    _remove_media(clip.subtitle_path)
    db.delete(clip)
    audit(db, actor, "clip.delete", "clip", clip_id)
    db.commit()


@router.post("/sources/{source_id}/archive")
def archive_source(source_id: int = PathParam(gt=0, le=2147483647), db: Session = Depends(session), actor: str = Depends(require_auth)) -> dict:
    """Soft-delete: keep the audit/job history, remove generated clips and uploaded media files."""
    lock_policy(db)
    lock_source(db, source_id)
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "source not found")
    clips = list(db.scalars(select(Clip).where(Clip.source_id == source_id)))
    if any(_published(db, clip.id) for clip in clips):
        raise HTTPException(409, "source clips are referenced by publications")
    if db.scalar(select(Job.id).where(Job.source_id == source_id, Job.status.in_(ACTIVE)).limit(1)):
        raise HTTPException(409, "source has active jobs")
    for clip in clips:
        _remove_media(clip.output_path)
        _remove_media(clip.subtitle_path)
        db.delete(clip)
    folder = (Path(settings.media_root).resolve() / "clips" / f"source-{source_id}")
    if folder.is_dir() and not folder.is_symlink():
        shutil.rmtree(folder)
    media = Path(source.media_path).resolve() if source.media_path else None
    if media and media.is_relative_to(Path(settings.media_root).resolve()) and media.is_file() and source.platform == "upload":
        media.unlink()
    source.media_path, source.status = None, "archived"
    audit(db, actor, "source.archive", "source", source_id, clips_removed=len(clips))
    db.commit()
    return {"source_id": source_id, "status": source.status, "clips_removed": len(clips)}
