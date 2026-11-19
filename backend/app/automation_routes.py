"""Authenticated automation controls and audit queries."""
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .audit import audit
from .auth import require_auth
from .automation import (
    ACTIVE,
    SCHEDULE_FIELDS,
    TERMINAL,
    Policy,
    ScheduleInput,
    ScheduleOutput,
    budget_used,
    get_policy,
    job_limits,
    lock_policy,
    update_workflow,
)
from .config import settings
from .db import session
from .models import (
    AuditEvent,
    AutomationPolicy,
    Clip,
    DiscoverySchedule,
    Job,
    Publication,
    Source,
    now,
)
from .publications import latest_rights, lock_source, transition
from .publishing import utc
from .rights import can_ingest_and_publish
from .scheduler import authorized_media, enqueue_processing
from .schemas import safe_media_path

router = APIRouter(prefix="/api/v1")
JobStatus = Literal["queued", "running", "cancel_requested", "succeeded", "failed", "dead_letter", "cancelled"]
JobKind = Literal["process_source", "quality_check", "discovery", "publish", "analytics", "optimize"]


def job_read(job: Job) -> dict:
    fields = ("id", "kind", "source_id", "workflow_id", "publication_id", "replay_of_id", "status",
              "attempts", "max_attempts", "run_after", "deadline_at", "expires_at", "wait_reason", "error_class",
              "last_error", "result", "created_at", "updated_at")
    return {name: getattr(job, name) for name in fields}


def get_job_or_404(db, job_id) -> Job:
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@router.get("/jobs")
def list_jobs(status: JobStatus | None = None, kind: JobKind | None = None,
              db: Session = Depends(session), _: str = Depends(require_auth)) -> list[dict]:
    query = select(Job).order_by(Job.created_at.desc(), Job.id).limit(100)
    if status:
        query = query.where(Job.status == status)
    if kind:
        query = query.where(Job.kind == kind)
    return [job_read(job) for job in db.scalars(query)]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(session), _: str = Depends(require_auth)) -> dict:
    return job_read(get_job_or_404(db, job_id))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, db: Session = Depends(session), actor: str = Depends(require_auth)) -> dict:
    lock_policy(db)
    db.execute(update(Job).where(Job.id == job_id).values(status=Job.status))
    job = get_job_or_404(db, job_id)
    if job.status not in (*ACTIVE, "cancelled"):
        raise HTTPException(409, "Job is not cancellable")
    if job.status == "queued":
        job.status, job.wait_reason = "cancelled", None
        update_workflow(db, job, "cancelled")
        if job.kind == "publish":
            publication = db.get(Publication, job.publication_id)
            publication.next_attempt_at = None
            transition(db, publication, "cancelled", actor)
        audit(db, actor, "job.cancel", "job", job.id, status=job.status)
    elif job.status == "running" or job.interruption == "emergency_stop":
        job.status, job.interruption = "cancel_requested", "operator_cancel"
        audit(db, actor, "job.cancel", "job", job.id, status=job.status)
    db.commit()
    return job_read(job)


def check_source_idle(db, source_id: int, *, replace_qc=False) -> None:
    referenced = db.scalar(select(Publication.id).join(Clip, Clip.id == Publication.clip_id).where(
        Clip.source_id == source_id).limit(1))
    if referenced:
        raise HTTPException(409, "source reruns are disabled while publications reference its clips")
    qc_jobs = list(db.scalars(select(Job).where(Job.kind == "quality_check", Job.source_id == source_id,
                                                Job.status.in_(ACTIVE))))
    if any(job.status != "queued" or not replace_qc for job in qc_jobs):
        raise HTTPException(409, "source quality checks are in progress")
    for job in qc_jobs:
        job.status = "cancelled"
        update_workflow(db, job, "superseded")
        audit(db, "operator", "job.cancel", "job", job.id, reason="superseded")


@router.post("/jobs/{job_id}/replay")
def replay_job(job_id: str, db: Session = Depends(session), actor: str = Depends(require_auth)) -> dict:
    lock_policy(db)
    original = get_job_or_404(db, job_id)
    existing = db.scalar(select(Job).where(Job.replay_of_id == original.id))
    if existing:
        return job_read(existing)
    if original.status not in TERMINAL:
        raise HTTPException(409, "Job is not eligible for replay")
    payload = original.payload or {}
    if original.kind == "process_source":
        lock_source(db, original.source_id)
        source = db.get(Source, original.source_id)
        rights = latest_rights(db, original.source_id)
        if not rights or not can_ingest_and_publish(rights) or not source or not authorized_media(source):
            raise HTTPException(409, "Authorized source media is required")
        if source.media_revision != payload.get("media_revision", 0):
            raise HTTPException(409, "Source media was replaced")
        check_source_idle(db, source.id, replace_qc=True)
        job = enqueue_processing(db, source)
    else:
        if original.kind == "publish":
            publication = db.get(Publication, original.publication_id)
            if not publication or publication.status not in {"failed", "blocked", "cancelled"} or publication.error_class == "ambiguous":
                raise HTTPException(409, "Publication needs reconciliation or is not eligible for replay")
            publication.next_attempt_at = now()
            publication.attempts = 0
            publication.error_class = publication.last_error = None
            transition(db, publication, "queued", actor)
        elif original.kind == "quality_check":
            current = db.scalar(select(Job.id).where(Job.kind == "process_source", Job.source_id == original.source_id,
                Job.status.in_(ACTIVE)).limit(1))
            pending = db.scalar(select(Clip.id).where(Clip.source_id == original.source_id, Clip.status == "qc_pending",
                Clip.evidence["job_id"].as_string() == payload["render_job_id"]).limit(1))
            if current or not pending:
                raise HTTPException(409, "Rendered clips are no longer eligible for quality checks")
        job = Job(id=uuid4().hex, kind=original.kind, source_id=original.source_id, workflow_id=original.workflow_id,
                  publication_id=original.publication_id, payload=payload, max_attempts=job_limits(original.kind)[0])
        db.add(job)
        update_workflow(db, job, "queued")
    job.replay_of_id = original.id
    audit(db, actor, "job.replay", "job", original.id, new_job_id=job.id, kind=job.kind)
    try:
        db.commit()
    except IntegrityError:
        raise HTTPException(409, "A job is already active") from None
    return job_read(job)


@router.get("/automation/policy", response_model=Policy)
def read_policy(db: Session = Depends(session), _: str = Depends(require_auth)) -> Policy:
    return get_policy(db)


@router.put("/automation/policy", response_model=Policy)
def put_policy(payload: Policy, db: Session = Depends(session), actor: str = Depends(require_auth)) -> Policy:
    row = lock_policy(db)
    old = Policy.model_validate(row.configuration).model_dump()
    new = payload.model_dump()
    row.configuration = new
    audit(db, actor, "policy.update", "automation_policy", row.id,
          changed_fields=sorted(key for key in new if old[key] != new[key]))
    db.commit()
    return payload


@router.get("/discovery/schedules", response_model=list[ScheduleOutput])
def list_schedules(db: Session = Depends(session), _: str = Depends(require_auth)) -> list[DiscoverySchedule]:
    return list(db.scalars(select(DiscoverySchedule).order_by(DiscoverySchedule.id)))


@router.post("/discovery/schedules", response_model=ScheduleOutput, status_code=201)
def create_schedule(payload: ScheduleInput, db: Session = Depends(session),
                    actor: str = Depends(require_auth)) -> DiscoverySchedule:
    lock_policy(db)
    schedule = DiscoverySchedule(**payload.model_dump())
    db.add(schedule)
    db.flush()
    audit(db, actor, "discovery.create", "discovery_schedule", schedule.id, platform=schedule.platform)
    db.commit()
    return schedule


class SchedulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Literal["youtube", "twitch"] | None = None
    query: str | None = None
    channel_allowlist: list[str] | None = None
    interval_minutes: int | None = None
    enabled: bool | None = None
    max_results: int | None = None


@router.patch("/discovery/schedules/{schedule_id}", response_model=ScheduleOutput)
def patch_schedule(schedule_id: int, payload: SchedulePatch, db: Session = Depends(session),
                   actor: str = Depends(require_auth)) -> DiscoverySchedule:
    lock_policy(db)
    schedule = db.get(DiscoverySchedule, schedule_id)
    if not schedule:
        raise HTTPException(404, "schedule not found")
    changes = payload.model_dump(exclude_unset=True)
    try:
        data = ScheduleInput.model_validate({**{key: getattr(schedule, key) for key in SCHEDULE_FIELDS}, **changes})
    except ValidationError:
        raise HTTPException(422, "invalid discovery schedule") from None
    for key, value in data.model_dump().items():
        setattr(schedule, key, value)
    if changes:
        schedule.next_run_at = now()
        audit(db, actor, "discovery.update", "discovery_schedule", schedule.id, changed_fields=sorted(changes))
    db.commit()
    return schedule


@router.delete("/discovery/schedules/{schedule_id}", status_code=204)
def delete_schedule(schedule_id: int, db: Session = Depends(session), actor: str = Depends(require_auth)) -> Response:
    lock_policy(db)
    schedule = db.get(DiscoverySchedule, schedule_id)
    if not schedule:
        raise HTTPException(404, "schedule not found")
    db.delete(schedule)
    audit(db, actor, "discovery.delete", "discovery_schedule", schedule_id)
    db.commit()
    return Response(status_code=204)


@router.get("/system/status")
def system_status(db: Session = Depends(session), _: str = Depends(require_auth)) -> dict:
    row = db.get(AutomationPolicy, 1)
    timestamp = now()
    heartbeat = row.worker_heartbeat_at if row else None
    depth: dict[str, dict[str, int]] = {}
    for status, kind, count in db.execute(select(Job.status, Job.kind, func.count()).group_by(Job.status, Job.kind)):
        depth.setdefault(status, {})[kind] = count
    daily, monthly = budget_used(db, timestamp)
    policy = get_policy(db)
    return {"emergency_stopped": bool(row and row.emergency_stopped), "queue_depth": depth,
            "worker_heartbeat_age_seconds": max(0, (timestamp - utc(heartbeat)).total_seconds()) if heartbeat else None,
            "budget": {"daily_used_cents": daily, "daily_ceiling_cents": policy.daily_budget_cents,
                       "monthly_used_cents": monthly, "monthly_ceiling_cents": policy.monthly_budget_cents}}


def set_emergency(db, actor: str, stopped: bool) -> dict:
    row = lock_policy(db)
    if row.emergency_stopped != stopped:
        row.emergency_stopped = stopped
        if stopped:
            db.execute(update(Job).where(Job.status == "running")
                       .values(status="cancel_requested", interruption="emergency_stop"))
        audit(db, actor, "system.emergency_stop" if stopped else "system.resume", "system", row.id)
    db.commit()
    return {"emergency_stopped": stopped}


@router.post("/system/emergency-stop")
def emergency_stop(db: Session = Depends(session), actor: str = Depends(require_auth)) -> dict:
    return set_emergency(db, actor, True)


@router.post("/system/resume")
def resume(db: Session = Depends(session), actor: str = Depends(require_auth)) -> dict:
    return set_emergency(db, actor, False)


@router.get("/audit")
def list_audit(entity: str | None = Query(default=None, max_length=32), limit: int = Query(default=100, ge=1, le=500),
               db: Session = Depends(session), _: str = Depends(require_auth)) -> list[dict]:
    query = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
    if entity:
        query = query.where(AuditEvent.entity == entity)
    return [{key: getattr(row, key) for key in ("id", "actor", "action", "entity", "entity_id", "details", "created_at")}
            for row in db.scalars(query)]


class MediaAttachment(BaseModel):
    media_path: str = Field(min_length=1, max_length=2000)


@router.post("/sources/{source_id}/media")
def attach_media(source_id: int, payload: MediaAttachment, db: Session = Depends(session),
                 actor: str = Depends(require_auth)) -> dict:
    lock_policy(db)
    lock_source(db, source_id)
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "source not found")
    rights = latest_rights(db, source_id)
    if not rights or not can_ingest_and_publish(rights):
        raise HTTPException(403, "Current rights evidence is required")
    try:
        path = Path(safe_media_path(payload.media_path))
        if not path.is_file() or not 0 < path.stat().st_size <= settings.max_upload_bytes:
            raise ValueError
    except (ValueError, OSError):
        raise HTTPException(422, "Authorized local media is missing or invalid") from None
    if db.scalar(select(Job.id).where(Job.source_id == source.id, Job.kind == "process_source",
                                       Job.status.in_(ACTIVE)).limit(1)):
        raise HTTPException(409, "source processing is in progress")
    check_source_idle(db, source_id, replace_qc=True)
    for clip in db.scalars(select(Clip).where(Clip.source_id == source_id)):
        db.delete(clip)
    source.media_path = str(path)
    source.media_sha256 = None
    source.media_revision += 1
    source.status = "authorized"
    audit(db, actor, "source.attach_media", "source", source.id, revision=source.media_revision)
    db.commit()
    return {"source_id": source.id, "status": source.status}


@router.post("/sources/{source_id}/rights/revoke")
def revoke_rights(source_id: int, db: Session = Depends(session), actor: str = Depends(require_auth)) -> dict:
    lock_policy(db)
    lock_source(db, source_id)
    source = db.get(Source, source_id)
    record = latest_rights(db, source_id)
    if not source or not record:
        raise HTTPException(404, "source rights not found")
    if not record.revoked:
        record.revoked = True
        source.status = "rights_review"
        audit(db, actor, "rights.revoke", "source", source_id, rights_id=record.id)
    db.commit()
    return {"source_id": source_id, "status": source.status, "can_publish": False}


@router.post("/clips/{clip_id}/approve")
def approve_clip(clip_id: int, db: Session = Depends(session), actor: str = Depends(require_auth)) -> dict:
    lock_policy(db)
    clip = db.get(Clip, clip_id)
    if not clip:
        raise HTTPException(404, "clip not found")
    if clip.status not in {"pending_review", "approved"}:
        raise HTTPException(409, "Clip must pass quality checks before approval")
    if clip.status != "approved":
        rights = latest_rights(db, clip.source_id)
        if not rights or not can_ingest_and_publish(rights):
            raise HTTPException(403, "Current rights do not authorize approval")
        clip.status = "approved"
        audit(db, actor, "clip.approve", "clip", clip.id, automatic=False)
        db.commit()
    return {"clip_id": clip.id, "status": clip.status}
