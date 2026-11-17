"""Leased database jobs. Media work runs off the event loop; DB writes are fenced."""
import asyncio
import logging
import signal
import time
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, or_, select, update

from .config import settings
from .db import SessionLocal
from .media import ingest_authorized, render_vertical
from .models import AuthSession, Clip, Job, RightsRecord, Source, WorkflowRun, now
from .pipeline import extract_candidates
from .rights import can_ingest_and_publish
from .schemas import safe_media_path
from .transcription import FasterWhisperTranscriber

logger = logging.getLogger(__name__)
LEASE_SECONDS = 120


def cleanup_sessions() -> int:
    cutoff = int(time.time()) - settings.session_retention_seconds
    with SessionLocal() as db:
        result = db.execute(delete(AuthSession).where(or_(AuthSession.expires_at < cutoff, AuthSession.revoked_at < cutoff)))
        db.commit()
        return result.rowcount


def claim_job(worker_id: str) -> str | None:
    timestamp = now()
    with SessionLocal() as db:
        # Expired claims consume an attempt, including crashes on the final attempt.
        expired = (Job.status == "running") & (Job.lease_expires_at <= timestamp)
        exhausted = list(db.scalars(select(Job).where(expired, Job.attempts >= Job.max_attempts)))
        for job in exhausted:
            changed = db.execute(update(Job).execution_options(synchronize_session=False).where(Job.id == job.id, expired).values(status="failed", locked_by=None, lease_expires_at=None, last_error="worker lease expired"))
            if changed.rowcount:
                db.execute(update(WorkflowRun).where(WorkflowRun.id == job.workflow_id).values(state="failed", error="worker lease expired"))
        db.execute(update(Job).execution_options(synchronize_session=False).where(expired, Job.attempts < Job.max_attempts).values(status="queued", locked_by=None, lease_expires_at=None))
        query = select(Job.id).where(Job.status == "queued", Job.run_after <= timestamp, Job.attempts < Job.max_attempts).order_by(Job.created_at).limit(1)
        if db.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        job_id = db.scalar(query)
        if job_id:
            result = db.execute(update(Job).execution_options(synchronize_session=False).where(Job.id == job_id, Job.status == "queued").values(status="running", attempts=Job.attempts + 1, locked_by=worker_id, locked_at=timestamp, lease_expires_at=timestamp + timedelta(seconds=LEASE_SECONDS)))
            if not result.rowcount:
                job_id = None
        db.commit()
        return job_id


def owned(job_id: str, worker_id: str):
    return (Job.id == job_id) & (Job.status == "running") & (Job.locked_by == worker_id) & (Job.lease_expires_at > now())


def renew_lease(job_id: str, worker_id: str) -> None:
    with SessionLocal() as db:
        db.execute(update(Job).execution_options(synchronize_session=False).where(owned(job_id, worker_id)).values(lease_expires_at=now() + timedelta(seconds=LEASE_SECONDS)))
        db.commit()


def process_job(job_id: str, worker_id: str) -> None:
    """Compute in claim-specific files, then atomically persist outputs and completion."""
    with SessionLocal() as db:
        job = db.scalar(select(Job).where(owned(job_id, worker_id)))
        if not job:
            return
        source = db.get(Source, job.source_id)
        rights = db.scalar(select(RightsRecord).where(RightsRecord.source_id == job.source_id).order_by(RightsRecord.created_at.desc()))
        if not source or not rights or not can_ingest_and_publish(rights):
            raise ValueError("processing authorization unavailable")
        media_path = safe_media_path(source.media_path or "")
        source_id, workflow_id = source.id, job.workflow_id
    with SessionLocal() as db:
        db.execute(update(WorkflowRun).where(WorkflowRun.id == workflow_id).values(state="transcribing"))
        db.commit()
    workspace = Path(settings.media_root) / "jobs" / job_id / worker_id
    ingested, digest, metadata = ingest_authorized(media_path, workspace / "input.mp4")
    segments = FasterWhisperTranscriber(settings.whisper_model_size).transcribe(ingested)
    candidates = extract_candidates(segments, limit=5)
    clips = []
    for index, candidate in enumerate(candidates, 1):
        subtitle = workspace / f"clip-{index}.srt"
        output = workspace / f"clip-{index}.mp4"
        duration_ms = max(1000, int((candidate.end - candidate.start) * 1000))
        hours, remaining = divmod(duration_ms, 3600000)
        minutes, remaining = divmod(remaining, 60000)
        seconds, milliseconds = divmod(remaining, 1000)
        subtitle.write_text(f"1\n00:00:00,000 --> {hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}\n{candidate.description}\n", encoding="utf-8")
        render_vertical(ingested, output, candidate.start, candidate.end, subtitle)
        clips.append(Clip(source_id=source_id, start_seconds=candidate.start, end_seconds=candidate.end, description=candidate.description, hook=candidate.hook, score=candidate.score, status="rendered", evidence={**candidate.evidence, "job_id": job_id}, output_path=str(output), subtitle_path=str(subtitle)))
    with SessionLocal() as db:
        # Conditional write fences out a stale worker after its lease was reclaimed.
        result = db.execute(update(Job).execution_options(synchronize_session=False).where(owned(job_id, worker_id)).values(status="succeeded", locked_by=None, lease_expires_at=None, last_error=None, result={"workflow_id": workflow_id, "source_id": source_id, "probe": asdict(metadata), "transcript_segments": len(segments), "clips_generated": len(clips)}))
        if not result.rowcount:
            db.rollback()
            return
        db.add_all(clips)
        db.execute(update(Source).where(Source.id == source_id).values(status="analyzed", media_sha256=digest))
        db.execute(update(WorkflowRun).where(WorkflowRun.id == workflow_id).values(state="edited", error=None))
        db.commit()


def fail_job(job_id: str, worker_id: str) -> None:
    with SessionLocal() as db:
        job = db.scalar(select(Job).where(owned(job_id, worker_id)))
        if not job:
            return
        failed = job.attempts >= job.max_attempts
        result = db.execute(update(Job).execution_options(synchronize_session=False).where(owned(job_id, worker_id)).values(status="failed" if failed else "queued", run_after=now() + timedelta(seconds=2 ** job.attempts), last_error="media processing failed", locked_by=None, lease_expires_at=None))
        if result.rowcount:
            db.execute(update(WorkflowRun).where(WorkflowRun.id == job.workflow_id).values(state="failed" if failed else "queued", error="media processing failed"))
        db.commit()


async def process_once() -> int:
    worker_id = uuid4().hex
    job_id = await asyncio.to_thread(claim_job, worker_id)
    if not job_id:
        return 0
    task = asyncio.create_task(asyncio.to_thread(process_job, job_id, worker_id))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=LEASE_SECONDS / 3)
            if not task.done():
                await asyncio.to_thread(renew_lease, job_id, worker_id)
                Path("/tmp/soe-worker-heartbeat").touch()  # noqa: S108
        await task
    except Exception:
        await asyncio.to_thread(fail_job, job_id, worker_id)
        logger.error("job_processing_failed")
    return 1


async def run_forever(interval_seconds: int = 10) -> None:
    settings.validate_production()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    next_cleanup = 0.0
    try:
        while not stop.is_set():
            try:
                if time.monotonic() >= next_cleanup:
                    await asyncio.to_thread(cleanup_sessions)
                    next_cleanup = time.monotonic() + 3600
                processed = await process_once()
                Path("/tmp/soe-worker-heartbeat").touch()  # noqa: S108
            except Exception:
                logger.error("worker_iteration_failed")
                processed = 0
            if not processed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
                except TimeoutError:
                    pass
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    asyncio.run(run_forever())
