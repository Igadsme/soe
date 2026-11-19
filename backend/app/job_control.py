"""Fenced queue state changes shared by the API and worker."""
from datetime import timedelta

from sqlalchemy import select, update

from .automation import record_schedule_status, retry_delay, update_workflow
from .models import Job, Publication, now
from .publications import transition
from .publishing import utc


def clear_claim(job: Job) -> None:
    job.locked_by = job.lease_expires_at = job.deadline_at = job.interruption = None


def settle_interrupted(db, job: Job) -> None:
    paused = job.interruption == "emergency_stop"
    job.status = "queued" if paused else "cancelled"
    job.error_class = job.last_error = None
    job.wait_reason = "emergency_stop" if paused else None
    if paused:
        job.attempts = max(0, job.attempts - 1)
        job.run_after = now()
    clear_claim(job)
    publication = db.get(Publication, job.publication_id) if job.kind == "publish" else None
    if publication:
        publication.attempts = job.attempts
        publication.next_attempt_at = job.run_after if paused else None
        transition(db, publication, job.status, "worker")
    update_workflow(db, job, job.status)
    if job.kind == "discovery":
        record_schedule_status(db, job, job.wait_reason or job.status)


def retry_or_dead_letter(db, job: Job, error_class: str, last_error: str, *,
                         retryable=True, delay=None) -> None:
    expired = job.expires_at is not None and utc(job.expires_at) <= now()
    if job.attempts >= job.max_attempts or expired:
        job.status = "dead_letter"
    else:
        job.status = "queued" if retryable else "failed"
    job.error_class, job.last_error, job.wait_reason = error_class, last_error, None
    clear_claim(job)
    if job.status == "queued":
        seconds = retry_delay(job.attempts) if delay is None else delay
        job.run_after = now() + timedelta(seconds=seconds)
    update_workflow(db, job, "queued" if job.status == "queued" else "failed", last_error)
    if job.kind == "discovery":
        record_schedule_status(db, job, job.status)


def finish_success(job: Job, result: dict) -> None:
    job.status = "succeeded"
    job.result = result
    job.wait_reason = job.last_error = job.error_class = None
    clear_claim(job)


def defer(job: Job, reason: str, *, seconds: int) -> None:
    job.status = "queued"
    job.wait_reason = reason
    job.error_class = job.last_error = None
    job.run_after = now() + timedelta(seconds=seconds)
    job.attempts = max(0, job.attempts - 1)
    clear_claim(job)


def own_claim(job_id: str, worker_id: str, *, interrupted=False):
    status = Job.status.in_(("running", "cancel_requested")) if interrupted else Job.status == "running"
    return (Job.id == job_id) & status & (Job.locked_by == worker_id) & (Job.lease_expires_at > now())


def lock_claim(db, job_id: str, worker_id: str, *, interrupted=False) -> Job | None:
    clause = own_claim(job_id, worker_id, interrupted=interrupted)
    db.execute(update(Job).where(clause).values(locked_at=Job.locked_at))
    return db.scalar(select(Job).where(clause).execution_options(populate_existing=True))
