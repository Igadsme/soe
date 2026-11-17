"""Leased database jobs. Media work runs off the event loop; DB writes are fenced."""
import asyncio
import logging
import shutil
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
from .models import (
    AuthSession,
    Clip,
    Job,
    Publication,
    RightsRecord,
    Source,
    WorkflowRun,
    now,
)
from .observability import JOB_DEADLINE_EXCEEDED
from .pipeline import extract_candidates
from .rights import can_ingest_and_publish
from .schemas import safe_media_path
from .subtitles import srt_timestamp
from .transcription import FasterWhisperTranscriber

logger = logging.getLogger(__name__)
LEASE_SECONDS = 120


def cleanup_sessions() -> int:
    cutoff = int(time.time()) - settings.session_retention_seconds
    with SessionLocal() as db:
        result = db.execute(delete(AuthSession).where(or_(AuthSession.expires_at < cutoff, AuthSession.revoked_at < cutoff, AuthSession.last_seen_at < int(time.time()) - settings.session_idle_seconds - settings.session_retention_seconds)))
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
        retry_ids = select(Job.workflow_id).where(expired, Job.attempts < Job.max_attempts)
        db.execute(update(WorkflowRun).where(WorkflowRun.id.in_(retry_ids)).values(state="queued", error="worker lease expired"))
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


def cleanup_unreferenced_output_folders(source_id: int, keep_workflow_id: int) -> int:
    root = (Path(settings.media_root).resolve() / "clips" / f"source-{source_id}").resolve()
    if not root.is_dir():
        return 0
    with SessionLocal() as db:
        if db.scalar(select(Publication.id).join(Clip, Clip.id == Publication.clip_id).where(Clip.source_id == source_id).limit(1)):
            return 0
    keep = (root / f"run-{keep_workflow_id}").resolve()
    removed = 0
    for folder in root.glob("run-*"):
        folder = folder.resolve()
        if folder == keep or root not in folder.parents:
            continue
        shutil.rmtree(folder, ignore_errors=False)
        removed += 1
    return removed


def _process_job(job_id: str, worker_id: str) -> None:
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
        db.execute(update(WorkflowRun).where(WorkflowRun.id == workflow_id, select(Job.id).where(owned(job_id, worker_id)).exists()).values(state="transcribing"))
        db.commit()
    workspace = Path(settings.media_root) / "clips" / f"source-{source_id}" / f"run-{workflow_id}" / worker_id
    ingested, digest, metadata = ingest_authorized(media_path, workspace / "input.mp4")
    segments = FasterWhisperTranscriber(settings.whisper_model_size).transcribe(ingested)
    candidates = extract_candidates(segments, limit=5)
    clips = []
    for index, candidate in enumerate(candidates, 1):
        subtitle = workspace / f"clip-{index}.srt"
        output = workspace / f"clip-{index}.mp4"
        subtitle.write_text(f"1\n00:00:00,000 --> {srt_timestamp(max(1, candidate.end - candidate.start))}\n{candidate.description}\n", encoding="utf-8")
        render_vertical(ingested, output, candidate.start, candidate.end, subtitle)
        clips.append(Clip(source_id=source_id, start_seconds=candidate.start, end_seconds=candidate.end, description=candidate.description, hook=candidate.hook, score=candidate.score, status="rendered", evidence={**candidate.evidence, "job_id": job_id}, output_path=str(output), subtitle_path=str(subtitle)))
    with SessionLocal() as db:
        # Conditional write fences out a stale worker after its lease was reclaimed.
        result = db.execute(update(Job).execution_options(synchronize_session=False).where(owned(job_id, worker_id)).values(status="succeeded", locked_by=None, lease_expires_at=None, last_error=None, result={"workflow_id": workflow_id, "source_id": source_id, "probe": asdict(metadata), "transcript_segments": len(segments), "clips_generated": len(clips)}))
        if not result.rowcount:
            db.rollback()
            return
        db.execute(delete(Clip).where(Clip.source_id == source_id))
        db.add_all(clips)
        db.execute(update(Source).where(Source.id == source_id).values(status="analyzed", media_sha256=digest))
        db.execute(update(WorkflowRun).where(WorkflowRun.id == workflow_id).values(state="edited", error=None))
        db.commit()
    cleanup_unreferenced_output_folders(source_id, workflow_id)


def process_job(job_id: str, worker_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        workspace = Path(settings.media_root) / "clips" / f"source-{job.source_id}" / f"run-{job.workflow_id}" / worker_id if job else None
    try:
        _process_job(job_id, worker_id)
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            if workspace and (not job or job.status != "succeeded"):
                shutil.rmtree(workspace, ignore_errors=True)
    except BaseException:
        if workspace:
            shutil.rmtree(workspace, ignore_errors=True)
        raise


def fail_job(job_id: str, worker_id: str, last_error: str = "media processing failed") -> None:
    with SessionLocal() as db:
        job = db.scalar(select(Job).where(owned(job_id, worker_id)))
        if not job:
            return
        failed = job.attempts >= job.max_attempts
        result = db.execute(update(Job).execution_options(synchronize_session=False).where(owned(job_id, worker_id)).values(status="failed" if failed else "queued", run_after=now() + timedelta(seconds=2 ** job.attempts), last_error=last_error, locked_by=None, lease_expires_at=None))
        if result.rowcount:
            db.execute(update(WorkflowRun).where(WorkflowRun.id == job.workflow_id).values(state="failed" if failed else "queued", error=last_error))
        db.commit()


async def process_once() -> int:
    worker_id = uuid4().hex
    job_id = await asyncio.to_thread(claim_job, worker_id)
    if not job_id:
        return 0
    deadline = settings.processing_deadline_floor_seconds
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job:
            source = db.get(Source, job.source_id)
            if source and source.media_path:
                try:
                    from .media import probe
                    duration = probe(Path(source.media_path)).duration
                    deadline = max(settings.processing_deadline_floor_seconds, settings.processing_deadline_duration_multiplier * duration)
                except Exception:
                    logger.warning("job_deadline_probe_failed", extra={"job_id": job_id})
    task = asyncio.create_task(asyncio.to_thread(process_job, job_id, worker_id))
    started = time.monotonic()
    try:
        while not task.done():
            remaining = deadline - (time.monotonic() - started)
            if remaining <= 0:
                await asyncio.to_thread(fail_job, job_id, worker_id, "processing deadline exceeded")
                task.cancel()
                JOB_DEADLINE_EXCEEDED.inc()
                logger.warning("job_processing_deadline_exceeded", extra={"job_id": job_id, "deadline_seconds": round(deadline, 2)})
                break
            await asyncio.wait({task}, timeout=min(LEASE_SECONDS / 3, remaining))
            if not task.done():
                if time.monotonic() - started < deadline:
                    await asyncio.to_thread(renew_lease, job_id, worker_id)
                    Path("/tmp/soe-worker-heartbeat").touch()  # noqa: S108
        if not task.done() or task.cancelled():
            return 1
        await task
    except Exception:
        import sentry_sdk
        sentry_sdk.capture_exception()
        await asyncio.to_thread(fail_job, job_id, worker_id)
        logger.error("job_processing_failed")
    return 1


async def run_forever(interval_seconds: int = 10) -> None:
    from .logging_config import configure_logging
    from .observability import init_sentry
    configure_logging()
    init_sentry()
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
