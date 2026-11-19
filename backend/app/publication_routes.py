"""Authenticated publication queue and audit endpoints."""
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Path as PathParam
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import publishing
from .auth import require_auth
from .config import settings
from .db import session
from .media import sha256_file
from .models import Clip, ConnectedAccount, Job, Publication, PublicationEvent, now
from .publications import latest_rights, lock_source, publication_job, transition
from .rights import can_publish_to
from .schemas import safe_media_path

router = APIRouter(prefix="/api/v1")


class PublicationRequest(BaseModel):
    platform: publishing.Platform
    account_id: int = Field(gt=0, le=2147483647)
    title: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=5000)
    privacy_status: Literal["private", "unlisted"] = "private"
    scheduled_at: datetime | None = None
    tiktok_privacy_level: Literal["SELF_ONLY", "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR"] | None = None
    instagram_visibility_acknowledged: bool = False


def privacy_for(payload: PublicationRequest) -> str:
    if payload.platform == "youtube":
        return payload.privacy_status
    if payload.privacy_status != "private":
        raise HTTPException(422, "Unlisted visibility is available for YouTube only")
    if payload.platform == "tiktok":
        return payload.tiktok_privacy_level or "SELF_ONLY"
    if not (payload.instagram_visibility_acknowledged or settings.publishing_dry_run):
        raise HTTPException(422, "Acknowledge that Instagram Reels use the account’s visibility")
    return "account"


def summary(row: Publication) -> dict:
    return {
        "id": row.id, "clip_id": row.clip_id, "account_id": row.account_id, "platform": row.platform,
        "status": row.status, "external_id": row.external_id, "external_url": row.external_url,
        "title": row.title, "description": row.description, "privacy_status": row.privacy_status,
        "scheduled_at": row.scheduled_at, "attempts": row.attempts, "next_attempt_at": row.next_attempt_at,
        "upload_progress": {"bytes_sent": row.bytes_sent, "bytes_total": row.bytes_total},
        "error_class": row.error_class, "last_error": row.last_error,
        "error": "publication failed" if row.error or row.last_error else None,
        "published_at": row.published_at, "created_at": row.created_at, "updated_at": row.updated_at,
        "dry_run": row.dry_run,
    }


def queued(row: Publication) -> dict:
    return {"publication_id": row.id, "status": row.status}


@router.post("/clips/{clip_id}/publish")
def queue_publication(payload: PublicationRequest, clip_id: int = PathParam(gt=0, le=2147483647),
                      db: Session = Depends(session), actor: str = Depends(require_auth)) -> dict:
    platform = payload.platform
    if not settings.publishing_allowed(platform):
        raise HTTPException(409, "Publishing is disabled for this platform")
    clip = db.get(Clip, clip_id)
    if not clip:
        raise HTTPException(404, "clip not found")
    rights = latest_rights(db, clip.source_id)
    if not rights or not can_publish_to(rights, platform):
        raise HTTPException(403, "Current rights do not authorize publication to this platform")
    account = db.get(ConnectedAccount, payload.account_id)
    if account is None or account.platform != platform or account.status != "connected":
        raise HTTPException(409, "A connected account for this platform is required")
    title = payload.title if payload.title is not None else clip.hook.replace("<", "").replace(">", "").strip()[:100] or "Clip"
    fallback = clip.description.replace("<", "").replace(">", "") if platform == "youtube" else clip.description
    if platform == "instagram":
        fallback = fallback[:2200]
    description = payload.description if payload.description is not None else fallback.encode()[:5000].decode(errors="ignore")
    privacy = privacy_for(payload)
    try:
        publishing.validate_metadata(platform, title, description, privacy)
    except publishing.PublishingUnavailable as exc:
        raise HTTPException(422, str(exc)) from None
    try:
        media = Path(safe_media_path(clip.output_path or ""))
        if clip.status != "rendered" or not media.is_file():
            raise ValueError
        total = media.stat().st_size
        if total <= 0:
            raise ValueError
        digest = sha256_file(media)
    except (ValueError, OSError):
        raise HTTPException(422, "Clip must have a rendered local media file") from None
    lock_source(db, clip.source_id)
    active_processing = db.scalar(select(Job.id).where(
        Job.kind == "process_source", Job.source_id == clip.source_id, Job.status.in_(["queued", "running"]),
    ).limit(1))
    if active_processing or db.scalar(select(Clip.id).where(Clip.id == clip.id)) is None:
        raise HTTPException(409, "Rendered clip is being replaced by source processing")
    key = sha256(f"{clip.id}:{platform}:{account.id}:{digest}".encode()).hexdigest()
    existing = db.scalar(select(Publication).where(Publication.idempotency_key == key))
    if existing:
        return queued(existing)
    timestamp = now()
    scheduled = publishing.utc(payload.scheduled_at) if payload.scheduled_at else None
    run_after = max(timestamp, scheduled) if scheduled else timestamp
    status = "scheduled" if run_after > timestamp else "queued"
    row = Publication(clip_id=clip.id, account_id=account.id, platform=platform, status=status,
                      idempotency_key=key, media_sha256=digest, bytes_total=total, title=title, description=description,
                      privacy_status=privacy, dry_run=settings.publishing_dry_run, scheduled_at=scheduled, next_attempt_at=run_after)
    try:
        db.add(row)
        db.flush()
        transition(db, row, status, actor, initial=True)
        db.add(Job(id=uuid4().hex, source_id=clip.source_id, publication_id=row.id, kind="publish",
                   run_after=run_after, max_attempts=settings.publishing_max_attempts))
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.scalar(select(Publication).where(Publication.idempotency_key == key))
        if row is None:
            raise HTTPException(409, "Publication could not be queued") from None
    return queued(row)


@router.get("/publications")
def publications(db: Session = Depends(session), _: str = Depends(require_auth)) -> list[dict]:
    return [summary(row) for row in db.scalars(select(Publication).order_by(Publication.id.desc()))]


def publication_or_404(db, publication_id: int) -> Publication:
    row = db.get(Publication, publication_id)
    if row is None:
        raise HTTPException(404, "Publication not found")
    return row


@router.post("/publications/{publication_id}/retry")
def retry(publication_id: int, db: Session = Depends(session), actor: str = Depends(require_auth)) -> dict:
    job = publication_job(db, publication_id)
    row = publication_or_404(db, publication_id)
    if not job or row.status != "failed" or row.error_class not in publishing.RETRYABLE:
        raise HTTPException(409, "Publication is not eligible for retry")
    if not settings.publishing_allowed(row.platform):
        raise HTTPException(409, "Publishing is disabled for this platform")
    timestamp = now()
    changed = db.execute(update(Job).where(Job.id == job.id, Job.status == "failed").values(
        status="queued", attempts=0, run_after=timestamp, last_error=None,
    ))
    if not changed.rowcount:
        raise HTTPException(409, "Publication is not eligible for retry")
    row.attempts = 0
    row.next_attempt_at = timestamp
    row.error_class = row.last_error = None
    transition(db, row, "queued", actor)
    db.commit()
    return queued(row)


@router.post("/publications/{publication_id}/cancel")
def cancel(publication_id: int, db: Session = Depends(session), actor: str = Depends(require_auth)) -> dict:
    job = publication_job(db, publication_id)
    row = publication_or_404(db, publication_id)
    if not job or row.status not in {"queued", "scheduled"} or row.upload_started_at is not None:
        raise HTTPException(409, "Publication can only be cancelled before sending media")
    changed = db.execute(update(Job).where(Job.id == job.id, Job.status == "queued").values(status="cancelled"))
    if not changed.rowcount:
        raise HTTPException(409, "Publication has already been claimed")
    row.next_attempt_at = None
    transition(db, row, "cancelled", actor)
    db.commit()
    return queued(row)


@router.get("/publications/{publication_id}/events")
def events(publication_id: int, db: Session = Depends(session), _: str = Depends(require_auth)) -> list[dict]:
    publication_or_404(db, publication_id)
    query = select(PublicationEvent).where(PublicationEvent.publication_id == publication_id).order_by(PublicationEvent.id)
    return [{"from_status": row.from_status, "to_status": row.to_status, "actor": row.actor,
             "error_class": row.error_class, "created_at": row.created_at} for row in db.scalars(query)]
