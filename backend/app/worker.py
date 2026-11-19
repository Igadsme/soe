"""Durable worker with fenced writes and killable media children."""
import asyncio
import logging
import math
import shutil
import signal
import time
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, update

from . import analytics_jobs, scheduler, templates
from . import signals as source_signals
from .audit import audit
from .automation import (
    PROCESSING_CATEGORY,
    PROCESSING_KINDS,
    Policy,
    budget_used,
    can_afford,
    job_limits,
    lock_policy,
    record_schedule_status,
    update_workflow,
)
from .config import settings
from .db import SessionLocal
from .integrations import ProviderNotConfigured
from .job_control import (
    defer,
    finish_success,
    lock_claim,
    own_claim,
    retry_or_dead_letter,
    settle_interrupted,
)
from .media import ingest_authorized, render_vertical
from .media_tasks import (
    probe_source,
    quality_check,
    render_single,
    render_subtitle,
    run_media_task,
)
from .models import (
    AnalyticsCollection,
    AuthSession,
    Clip,
    DiscoverySchedule,
    Job,
    LedgerEntry,
    MetricEvent,
    Publication,
    Source,
    WorkflowRun,
    now,
)
from .observability import JOB_DEADLINE_EXCEEDED, JOB_RECLAIMED
from .pipeline import extract_candidates_v2 as extract_candidates
from .publication_worker import PublisherJob
from .publications import latest_rights, lock_source, recover_expired
from .publishing import PublishingUnavailable, utc
from .rights import can_ingest_and_publish
from .schemas import safe_media_path
from .subtitles import build_cues, timed_words, write_ass, write_srt
from .transcription import FasterWhisperTranscriber

logger = logging.getLogger(__name__)
LEASE_SECONDS = 120
POLL_SECONDS = 0.25


def cleanup_sessions() -> int:
    cutoff = int(time.time()) - settings.session_retention_seconds
    with SessionLocal() as db:
        result = db.execute(delete(AuthSession).where(or_(AuthSession.expires_at < cutoff, AuthSession.revoked_at < cutoff, AuthSession.last_seen_at < int(time.time()) - settings.session_idle_seconds - settings.session_retention_seconds)))
        db.commit()
        return result.rowcount


def reclaim_stuck(db, timestamp):
    cutoff = timestamp - timedelta(seconds=settings.media_terminate_grace_seconds + POLL_SECONDS)
    expired = (Job.status.in_(("running", "cancel_requested"))) & or_(
        Job.lease_expires_at <= timestamp, Job.deadline_at <= cutoff,
    )
    queued_timeout = (Job.status == "queued") & (Job.expires_at <= timestamp)
    query = select(Job).where(or_(expired, queued_timeout))
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    for job in db.scalars(query):
        lease_expired = job.lease_expires_at and utc(job.lease_expires_at) <= timestamp
        reason = "lease_expired" if lease_expired else "deadline_exceeded"
        if job.status == "cancel_requested":
            settle_interrupted(db, job)
        else:
            error = "worker lease expired" if lease_expired else "job deadline exceeded"
            retry_or_dead_letter(db, job, reason, error, delay=0 if lease_expired else None)
            if job.kind == "publish":
                recover_expired(db, job, job.status != "queued")
        JOB_RECLAIMED.labels(job.kind, reason).inc()
        logger.warning("stuck_job_reclaimed", extra={"job_id": job.id, "kind": job.kind, "reason": reason})


def claim_job(worker_id: str) -> str | None:
    with SessionLocal() as db:
        row = lock_policy(db)
        timestamp = now()
        row.worker_heartbeat_at = timestamp
        reclaim_stuck(db, timestamp)
        # Reclaimed jobs become due at "now"; re-read the clock so they are claimable immediately.
        timestamp = now()
        if row.emergency_stopped:
            db.commit()
            return None

        policy = Policy.model_validate(row.configuration)
        counts = dict(db.execute(select(Job.kind, func.count()).where(Job.status.in_(("running", "cancel_requested")))
                                 .group_by(Job.kind)).all())
        kinds = [kind for kind in ("discovery", "analytics", "optimize") if counts.get(kind, 0) < 1]
        if sum(counts.get(kind, 0) for kind in PROCESSING_KINDS) < policy.max_concurrent_processing_jobs:
            kinds.extend(PROCESSING_KINDS)
        if counts.get("publish", 0) < policy.max_concurrent_publish_jobs:
            kinds.append("publish")
        query = (select(Job).where(Job.status == "queued", Job.run_after <= timestamp, Job.attempts < Job.max_attempts,
                 Job.kind.in_(kinds)).order_by(Job.created_at, Job.id).limit(1))
        if db.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        job = db.scalar(query)
        if job is not None:
            job.status = "running"
            job.attempts += 1
            job.locked_by, job.locked_at = worker_id, timestamp
            job.lease_expires_at = timestamp + timedelta(seconds=LEASE_SECONDS)
            deadline = timestamp + timedelta(seconds=job_limits(job.kind)[1])
            job.deadline_at = min(deadline, utc(job.expires_at)) if job.expires_at else deadline
            job.wait_reason = None
        db.commit()
        return job.id if job else None


def owned(job_id: str, worker_id: str):
    return own_claim(job_id, worker_id)


def renew_lease(job_id: str, worker_id: str) -> None:
    with SessionLocal() as db:
        row = lock_policy(db)
        if not row.emergency_stopped:
            db.execute(update(Job).where(owned(job_id, worker_id), Job.deadline_at > now())
                       .values(lease_expires_at=now() + timedelta(seconds=LEASE_SECONDS)))
        row.worker_heartbeat_at = now()
        db.commit()


def cleanup_unreferenced_output_folders(source_id: int, keep_workflow_id: int) -> int:
    root = (Path(settings.media_root).resolve() / "clips" / f"source-{source_id}").resolve()
    if not root.is_dir():
        return 0
    with SessionLocal() as db:
        if db.scalar(select(Publication.id).join(Clip, Clip.id == Publication.clip_id).where(Clip.source_id == source_id).limit(1)):
            return 0
        # Only remove older workflow folders. A new claim may already be rendering.
        older = db.scalars(select(WorkflowRun.id).where(WorkflowRun.source_id == source_id, WorkflowRun.id < keep_workflow_id)).all()
    removed = 0
    for run_id in older:
        folder = root / f"run-{run_id}"
        if folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder)
            removed += 1
    return removed


def analyze_signals(path: Path, metadata):
    """Stage 1 signals are best-effort: a failure falls back to transcript-only ranking, recorded in evidence."""
    try:
        return source_signals.analyze(path, metadata.duration, metadata.has_audio), None
    except Exception as exc:  # noqa: BLE001 - FFmpeg/numpy failure must not block transcript-based clipping
        return None, type(exc).__name__


def render_source(media_path: str, workspace: Path, limit: int, expected_duration: float | None) -> dict:
    ingested, digest, metadata = ingest_authorized(media_path, workspace / "input.mp4")
    if expected_duration is not None and metadata.duration != expected_duration:
        raise ValueError("source duration changed during admission")
    sig, signal_error = analyze_signals(ingested, metadata)
    segments = FasterWhisperTranscriber(settings.whisper_model_size).transcribe(ingested) if metadata.has_audio else []
    candidates = extract_candidates(segments, sig, limit=limit, min_seconds=settings.highlight_min_seconds,
                                       max_seconds=min(settings.max_clip_seconds, settings.highlight_target_max_seconds),
                                       duration=metadata.duration)
    template = templates.get(settings.edit_template)
    clips = []
    for index, candidate in enumerate(candidates, 1):
        subtitle = workspace / f"clip-{index}.srt"
        styled = workspace / f"clip-{index}.ass"
        output = workspace / f"clip-{index}.mp4"
        length = candidate.end - candidate.start
        words = timed_words([s for s in segments if getattr(s, "end", 0) > candidate.start and getattr(s, "start", 0) < candidate.end],
                            candidate.start, candidate.end)
        cues = build_cues(words, length)
        if not cues:
            render_subtitle(subtitle, length, candidate.description)
        else:
            write_srt(subtitle, cues)
        write_ass(styled, cues or [], template, length, settings.brand_text)
        crop = sig.motion.action_center(candidate.start, candidate.end) if sig is not None and sig.motion else None
        render_vertical(ingested, output, candidate.start, candidate.end, styled if cues or settings.brand_text else subtitle,
                        template, crop)
        evidence = {**candidate.evidence, "template": template.name, "crop_center": round(crop, 3) if crop is not None else None,
                    "subtitle_cues": len(cues), "word_timestamps": any(getattr(s, "words", ()) for s in segments)}
        clips.append({"start_seconds": candidate.start, "end_seconds": candidate.end,
                      "description": candidate.description, "hook": candidate.hook, "score": candidate.score,
                      "evidence": evidence, "output_path": str(output), "subtitle_path": str(subtitle)})
    return {"probe": asdict(metadata), "digest": digest, "transcript_segments": len(segments), "clips": clips,
            "signals": sig.summary() if sig is not None else {"unavailable": signal_error}}


def source_inputs(job_id: str, worker_id: str):
    with SessionLocal() as db:
        job = db.scalar(select(Job).where(owned(job_id, worker_id)))
        if not job:
            return None
        source = db.get(Source, job.source_id)
        rights = latest_rights(db, job.source_id)
        revision = (job.payload or {}).get("media_revision", 0)
        if not source or not rights or not can_ingest_and_publish(rights) or source.media_revision != revision:
            raise ValueError("processing authorization unavailable")
        return source, job.workflow_id


def reserve_processing(job_id, worker_id, source_id, duration):
    with SessionLocal() as db:
        row = lock_policy(db)
        job = lock_claim(db, job_id, worker_id)
        if not job or row.emergency_stopped:
            return None
        policy = Policy.model_validate(row.configuration)
        timestamp = now()
        if (job.payload or {}).get("automated") and not policy.auto_process_authorized:
            defer(job, "automation_disabled", seconds=60)
            db.commit()
            return None
        lock_source(db, source_id)
        rights = latest_rights(db, source_id)
        if not rights or not can_ingest_and_publish(rights, timestamp):
            raise ValueError("processing authorization unavailable")
        estimate = math.ceil(duration * settings.processing_cents_per_minute / 60)
        if not can_afford(policy, budget_used(db, timestamp), estimate):
            defer(job, "budget_exhausted", seconds=60)
            update_workflow(db, job, "queued", "budget_exhausted")
            db.commit()
            return None
        workflow = db.get(WorkflowRun, job.workflow_id)
        workflow.cost_cents += estimate
        workflow.state, workflow.error = "transcribing", None
        db.add(LedgerEntry(kind="expense", category=PROCESSING_CATEGORY, cents=estimate, verified=False, provenance="estimated",
                           note=f"estimated processing expense for job {job.id}, attempt {job.attempts}"))
        job.expires_at = job.expires_at or timestamp + timedelta(seconds=settings.workflow_timeout_seconds)
        deadline = utc(job.locked_at) + timedelta(seconds=max(
            settings.processing_deadline_floor_seconds, settings.processing_deadline_duration_multiplier * duration))
        job.deadline_at = min(deadline, utc(job.expires_at))
        db.commit()
        return policy.max_clips_per_source


def save_render(job_id: str, worker_id: str, result: dict) -> bool:
    with SessionLocal() as db:
        job = lock_claim(db, job_id, worker_id)
        if not job or (job.deadline_at and utc(job.deadline_at) <= now()):
            return False
        lock_source(db, job.source_id)
        rights = latest_rights(db, job.source_id)
        if not rights or not can_ingest_and_publish(rights):
            raise ValueError("processing authorization unavailable")
        entries = result["clips"]
        db.execute(delete(Clip).where(Clip.source_id == job.source_id))
        for entry in entries:
            evidence = {**entry["evidence"], "job_id": job.id}
            db.add(Clip(source_id=job.source_id, status="qc_pending", **{**entry, "evidence": evidence}))
        source = db.get(Source, job.source_id)
        source.status, source.media_sha256 = "analyzed", result["digest"]
        update_workflow(db, job, "qc_pending")
        qc = scheduler.enqueue(db, "quality_check", key=f"qc:{job.id}", source_id=job.source_id,
                               workflow_id=job.workflow_id, expires_at=job.expires_at,
                               payload={"render_job_id": job.id, "source_has_audio": result["probe"]["has_audio"]})
        finish_success(job, {"workflow_id": job.workflow_id, "source_id": job.source_id, "probe": result["probe"],
                             "transcript_segments": result["transcript_segments"], "clips_generated": len(entries),
                             "quality_check_job_id": qc.id})
        db.commit()
        return True


def process_job(job_id: str, worker_id: str) -> None:
    """Synchronous media entry point for callers that already own an isolated execution context."""
    values = source_inputs(job_id, worker_id)
    if not values:
        return
    source, workflow_id = values
    workspace = job_workspace(source.id, workflow_id, worker_id)
    saved = False
    try:
        result = render_source(safe_media_path(source.media_path or ""), workspace, 5, None)
        saved = save_render(job_id, worker_id, result)
        if saved:
            cleanup_unreferenced_output_folders(source.id, workflow_id)
    finally:
        if not saved:
            shutil.rmtree(workspace, ignore_errors=True)


def job_workspace(source_id, workflow_id, worker_id):
    return Path(settings.media_root).resolve() / "clips" / f"source-{source_id}" / f"run-{workflow_id}" / worker_id


async def execute_media(job_id, worker_id):
    values = source_inputs(job_id, worker_id)
    if not values:
        return
    source, workflow_id = values
    workspace = job_workspace(source.id, workflow_id, worker_id)
    media_path = safe_media_path(source.media_path or "")
    saved = False
    try:
        with TemporaryDirectory() as result_dir:
            result_file = Path(result_dir) / "result.json"
            metadata = await run_media_task(probe_source, (media_path,), result_file)
            limit = reserve_processing(job_id, worker_id, source.id, metadata["duration"])
            if limit is None:
                return
            result = await run_media_task(render_source, (media_path, workspace, limit, metadata["duration"]), result_file)
        saved = save_render(job_id, worker_id, result)
        if saved:
            cleanup_unreferenced_output_folders(source.id, workflow_id)
    finally:
        if not saved:
            shutil.rmtree(workspace, ignore_errors=True)


async def execute_render_clip(job_id, worker_id, snapshot):
    payload = snapshot.payload or {}
    with SessionLocal() as db:
        clip = db.get(Clip, payload.get("clip_id"))
        source = db.get(Source, snapshot.source_id)
        rights = latest_rights(db, snapshot.source_id)
        if (not clip or not source or not rights or not can_ingest_and_publish(rights)
                or source.media_revision != payload.get("media_revision", 0)):
            raise ValueError("processing authorization unavailable")
        previous = Path(safe_media_path(clip.output_path)) if clip.output_path else None
        ingested = previous.parent / "input.mp4" if previous and (previous.parent / "input.mp4").is_file() else None
        media_input = str(ingested or safe_media_path(source.media_path or ""))
        folder = previous.parent if previous else job_workspace(source.id, 0, "rerender")
        description = clip.description
    attempt = uuid4().hex[:8]
    output = folder / f"clip-{clip.id}-r{attempt}.mp4"
    subtitle = folder / f"clip-{clip.id}-r{attempt}.srt"
    start, end = float(payload["start_seconds"]), float(payload["end_seconds"])
    try:
        with TemporaryDirectory() as result_dir:
            result = await run_media_task(render_single, (media_input, str(output), str(subtitle), start, end, description),
                                          Path(result_dir) / "result.json")
        with SessionLocal() as db:
            job = lock_claim(db, job_id, worker_id)
            if not job or (job.deadline_at and utc(job.deadline_at) <= now()):
                raise ValueError("render claim lost")
            clip = db.get(Clip, payload["clip_id"])
            old = (clip.output_path, clip.subtitle_path)
            clip.start_seconds, clip.end_seconds = start, end
            clip.output_path, clip.subtitle_path = str(output), str(subtitle)
            clip.status = "qc_pending"
            clip.evidence = {**(clip.evidence or {}), "job_id": job.id, "edited_bounds": [start, end]}
            qc = scheduler.enqueue(db, "quality_check", key=f"qc:{job.id}", source_id=job.source_id,
                                   payload={"render_job_id": job.id, "source_has_audio": result["has_audio"]})
            finish_success(job, {"clip_id": clip.id, "quality_check_job_id": qc.id})
            db.commit()
        for path in old:
            if path and Path(path).resolve() != output.resolve():
                Path(path).unlink(missing_ok=True)
    except BaseException:
        output.unlink(missing_ok=True)
        subtitle.unlink(missing_ok=True)
        raise


async def execute_qc(job_id, worker_id, snapshot):
    payload = snapshot.payload
    with SessionLocal() as db:
        clips = db.scalars(select(Clip).where(Clip.source_id == snapshot.source_id, Clip.status == "qc_pending",
                         Clip.evidence["job_id"].as_string() == payload["render_job_id"])).all()
    items = [{"clip_id": clip.id, "output": safe_media_path(clip.output_path or ""),
              "subtitle": safe_media_path(clip.subtitle_path or "")} for clip in clips]
    with TemporaryDirectory() as result_dir:
        results = await run_media_task(quality_check, (items, payload["source_has_audio"]), Path(result_dir) / "result.json")
    with SessionLocal() as db:
        row = lock_policy(db)
        job = lock_claim(db, job_id, worker_id)
        if not job or row.emergency_stopped or utc(job.deadline_at) <= now():
            return
        policy = Policy.model_validate(row.configuration)
        failures = 0
        for result in results:
            clip = db.get(Clip, result["clip_id"])
            if not clip or clip.status != "qc_pending":
                continue
            reasons = result["reasons"]
            clip.evidence = {**clip.evidence, "qc_reasons": reasons, "qc_sha256": result["sha256"], "qc_metrics": result.get("metrics", {})}
            if reasons:
                clip.status = "qc_failed"
                failures += 1
            elif policy.approval_mode == "auto_after_qc" and clip.score >= policy.min_clip_score:
                clip.status = "approved"
                audit(db, "worker", "clip.approve", "clip", clip.id, automatic=True)
            else:
                clip.status = "pending_review"
        state = "qc_failed" if not results or failures == len(results) else "edited"
        update_workflow(db, job, state, "no clips passed quality checks" if state == "qc_failed" else None)
        finish_success(job, {"checked": len(results), "failed": failures})
        db.commit()


async def execute_metadata(job_id, worker_id, snapshot):
    payload = snapshot.payload or {}
    kind = snapshot.kind
    items, metrics, schedule = [], None, None
    result = {"status": "unavailable"}
    with SessionLocal() as db:
        if kind == "discovery":
            schedule = db.get(DiscoverySchedule, payload["schedule_id"])
        elif kind == "analytics":
            publication = db.get(Publication, snapshot.publication_id)
            account = db.get(analytics_jobs.ConnectedAccount, publication.account_id) if publication and publication.account_id else None
    if kind == "discovery":
        if not schedule or not schedule.enabled:
            result = {"status": "disabled"}
        else:
            try:
                adapter = scheduler.DISCOVERY_ADAPTERS[schedule.platform]
                items = await adapter.discover(schedule.query, schedule.channel_allowlist, schedule.max_results)
                result = {"status": "succeeded", "discovered": len(items)}
            except ProviderNotConfigured:
                result = {"status": "blocked: provider not configured"}
    elif kind == "analytics" and publication and publication.status == "succeeded":
        adapter = analytics_jobs.ADAPTERS.get(publication.platform)
        try:
            if not adapter:
                raise analytics_jobs.Unavailable("no analytics adapter for this platform")
            metrics = await adapter.collect(publication, account)
            result = {"status": "collected", "metrics": sorted(metrics)}
        except analytics_jobs.Unavailable as exc:
            metrics, result = None, {"status": "unavailable", "reason": str(exc)}

    with SessionLocal() as db:
        row = lock_policy(db)
        job = lock_claim(db, job_id, worker_id)
        if not job or row.emergency_stopped or utc(job.deadline_at) <= now():
            return
        if kind == "discovery":
            current = db.get(DiscoverySchedule, payload["schedule_id"])
            if current and current.enabled and schedule and all(
                    getattr(current, key) == getattr(schedule, key) for key in ("platform", "query", "channel_allowlist", "max_results")):
                if items:
                    result["created"] = scheduler.save_discovery(db, schedule, items, now())
                record_schedule_status(db, job, result["status"])
            else:
                result = {"status": "disabled" if not current or not current.enabled else "superseded"}
        elif kind == "analytics" and publication and publication.status == "succeeded":
            window = payload.get("window") or "initial"
            already = db.scalar(select(AnalyticsCollection.id).where(
                AnalyticsCollection.publication_id == job.publication_id, AnalyticsCollection.window == window))
            if already:
                result = {"status": "duplicate", "window": window}
            else:
                provider = getattr(analytics_jobs.ADAPTERS.get(publication.platform), "provider", publication.platform)
                db.add(AnalyticsCollection(publication_id=job.publication_id, window=window, provider=provider,
                                           status=result["status"], reason=result.get("reason")))
                if metrics:
                    db.add_all(MetricEvent(publication_id=job.publication_id, metric=metric, value=value, verified=True,
                                           source=f"{provider}_api", provenance="verified", window=window)
                               for metric, value in metrics.items())
                    scheduler.enqueue(db, "optimize", key=f"optimize:{job.id}", payload={"platform": publication.platform})
        elif kind == "optimize":
            result = analytics_jobs.optimize(db, payload["platform"])
        finish_success(job, result)
        db.commit()


def fail_job(job_id: str, worker_id: str, last_error: str = "media processing failed", *, error_class="processing") -> None:
    with SessionLocal() as db:
        job = lock_claim(db, job_id, worker_id)
        if job:
            retry_or_dead_letter(db, job, error_class, last_error)
            db.commit()


def job_control_state(job_id, worker_id):
    with SessionLocal() as db:
        job = db.scalar(select(Job).where(own_claim(job_id, worker_id, interrupted=True)))
        return (job.status, utc(job.deadline_at)) if job else (None, None)


def finish_interruption(job_id, worker_id):
    with SessionLocal() as db:
        job = lock_claim(db, job_id, worker_id, interrupted=True)
        if job and job.status == "cancel_requested":
            settle_interrupted(db, job)
            db.commit()


async def process_once() -> int:
    worker_id = uuid4().hex
    job_id = await asyncio.to_thread(claim_job, worker_id)
    if not job_id:
        return 0
    with SessionLocal() as db:
        job = db.get(Job, job_id)
    publisher = None
    if job.kind == "publish":
        publisher = PublisherJob(SessionLocal, lambda: owned(job_id, worker_id))
        work = publisher.execute()
    elif job.kind == "process_source":
        work = execute_media(job_id, worker_id)
    elif job.kind == "quality_check":
        work = execute_qc(job_id, worker_id, job)
    elif job.kind == "render_clip":
        work = execute_render_clip(job_id, worker_id, job)
    else:
        work = execute_metadata(job_id, worker_id, job)

    task = asyncio.create_task(work)
    next_renewal = time.monotonic() + LEASE_SECONDS / 3
    try:
        deadline = utc(job.deadline_at)
        while not task.done():
            wait = min(POLL_SECONDS, max(0.0, (deadline - now()).total_seconds()),
                       max(0.0, next_renewal - time.monotonic()))
            await asyncio.wait({task}, timeout=wait)
            status, latest_deadline = await asyncio.to_thread(job_control_state, job_id, worker_id)
            if status == "cancel_requested":
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.to_thread(finish_interruption, job_id, worker_id)
                return 1
            if status is None:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                return 1
            deadline = latest_deadline
            if deadline <= now():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if publisher:
                    publisher.finish(error=PublishingUnavailable("transient", "Publishing deadline exceeded"))
                else:
                    message = "processing deadline exceeded" if job.kind == "process_source" else "job deadline exceeded"
                    await asyncio.to_thread(fail_job, job_id, worker_id, message, error_class="deadline_exceeded")
                JOB_DEADLINE_EXCEEDED.inc()
                logger.warning("job_processing_deadline_exceeded", extra={"job_id": job_id, "kind": job.kind})
                return 1
            if time.monotonic() >= next_renewal and not task.done():
                await asyncio.to_thread(renew_lease, job_id, worker_id)
                Path("/tmp/soe-worker-heartbeat").touch()  # noqa: S108
                next_renewal = time.monotonic() + LEASE_SECONDS / 3
        await task
    except Exception:
        if publisher:
            publisher.finish(error=PublishingUnavailable("transient", "Publishing job interrupted"))
        else:
            is_media = job.kind in PROCESSING_KINDS
            await asyncio.to_thread(fail_job, job_id, worker_id,
                                    "media processing failed" if is_media else "provider job failed",
                                    error_class="processing" if is_media else "provider")
        logger.error("job_execution_failed", extra={"job_id": job_id, "kind": job.kind})
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return 1


async def scheduler_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(scheduler.scheduler_once, SessionLocal)
        except Exception:
            logger.error("scheduler_iteration_failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.scheduler_interval_seconds)
        except TimeoutError:
            pass


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
    schedule = asyncio.create_task(scheduler_loop(stop))
    next_cleanup = 0.0
    try:
        while not stop.is_set():
            try:
                if time.monotonic() >= next_cleanup:
                    await asyncio.to_thread(cleanup_sessions)
                    next_cleanup = time.monotonic() + 3600
                work = asyncio.create_task(process_once())
                stopping = asyncio.create_task(stop.wait())
                done, _ = await asyncio.wait({work, stopping}, return_when=asyncio.FIRST_COMPLETED)
                if work not in done:
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                    break
                stopping.cancel()
                await asyncio.gather(stopping, return_exceptions=True)
                processed = work.result()
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
        stop.set()
        await schedule
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    asyncio.run(run_forever())
