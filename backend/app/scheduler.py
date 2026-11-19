"""Enqueue due work from database state. Provider requests execute in workers."""
import logging
import math
import re
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import exists, func, select

from . import analytics_jobs
from .analytics_jobs import duration_band
from .automation import (
    ACTIVE,
    Policy,
    budget_used,
    can_afford,
    job_limits,
    lock_policy,
    platform_quota_used,
)
from .config import settings
from .integrations import DiscoveredItem, TwitchDiscovery, YouTubeDiscovery
from .models import (
    AnalyticsCollection,
    Clip,
    ConnectedAccount,
    DiscoverySchedule,
    Job,
    Publication,
    Source,
    WorkflowRun,
    now,
)
from .publication_routes import PublicationRequest, schedule_publication
from .publications import latest_rights, lock_source
from .publishing import utc
from .rights import can_ingest_and_publish, can_publish_to
from .schemas import safe_media_path

logger = logging.getLogger(__name__)
DISCOVERY_ADAPTERS = {"youtube": YouTubeDiscovery(), "twitch": TwitchDiscovery()}
DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", re.I)


def enqueue(db, kind, *, key=None, **fields) -> Job | None:
    digest = sha256(key.encode()).hexdigest() if key else None
    if digest and db.scalar(select(Job.id).where(Job.idempotency_key == digest)):
        return None
    job = Job(id=uuid4().hex, kind=kind, max_attempts=job_limits(kind)[0], idempotency_key=digest, **fields)
    db.add(job)
    return job


def enqueue_processing(db, source, *, idempotency_key=None, automated=False) -> Job:
    run = WorkflowRun(source_id=source.id, state="queued")
    db.add(run)
    db.flush()
    job = Job(id=uuid4().hex, source_id=source.id, workflow_id=run.id, idempotency_key=idempotency_key,
              max_attempts=job_limits("process_source")[0],
              payload={"media_revision": source.media_revision, "automated": automated})
    db.add(job)
    return job


def authorized_media(source) -> bool:
    try:
        return bool(source.media_path and Path(safe_media_path(source.media_path)).is_file())
    except (ValueError, OSError):
        return False


def rank_item(item: DiscoveredItem, query: str, channels: list[str], timestamp: datetime) -> dict:
    meta = item.metadata
    published = next((meta.get(key) for key in ("publishedAt", "published_at", "started_at", "created_at")
                      if meta.get(key)), None)
    try:
        age_days = max(0, (utc(timestamp) - utc(datetime.fromisoformat(published))).total_seconds() / 86400)
        recency = max(0, 1 - age_days / 30)
    except (ValueError, TypeError):
        recency = 0
    channel = str(meta.get("channelId") or meta.get("user_login") or meta.get("broadcaster_login") or "")
    allowed = channel in channels if item.platform == "youtube" else channel.lower() in {c.lower() for c in channels}
    words = set(re.findall(r"\w+", query.casefold()))
    text = " ".join(str(value) for value in (item.title, meta.get("description", ""), meta.get("game_name", "")))
    niche = len(words.intersection(re.findall(r"\w+", text.casefold()))) / max(1, len(words))
    raw = str(meta.get("duration") or "").removeprefix("PT")
    match = DURATION.fullmatch(raw) if raw else None
    duration = sum(int(value or 0) * units for value, units in zip(match.groups(), (3600, 60, 1), strict=True)) if match else None
    duration_fit = int(duration is not None and settings.qc_min_clip_seconds <= duration <= settings.max_source_seconds)
    try:
        views = int(meta.get("view_count") or meta.get("viewer_count") or 0)
        engagement = min(1, math.log10(1 + max(0, views)) / 6)
    except (ValueError, TypeError):
        engagement = 0
    factors = {"recency": recency, "channel_allowlist": int(allowed), "niche_keywords": niche,
               "duration_fit": duration_fit, "engagement": engagement}
    score = round(25 * recency + 25 * allowed + 25 * niche + 15 * duration_fit + 10 * engagement, 2)
    return {"score": score, "factors": factors}


def save_discovery(db, schedule: DiscoverySchedule, items: list[DiscoveredItem], timestamp) -> int:
    ranked = [(rank_item(item, schedule.query, schedule.channel_allowlist, timestamp), item) for item in items]
    ranked.sort(key=lambda pair: (-pair[0]["score"], pair[1].external_id))
    created = 0
    for ranking, item in ranked[:schedule.max_results]:
        source = db.scalar(select(Source).where(Source.external_id == item.external_id))
        if source is None:
            db.add(Source(platform=item.platform, external_id=item.external_id, title=item.title[:500],
                          url=item.url, status="rights_review", metadata_json={**item.metadata, "discovery_rank": ranking}))
            created += 1
        elif source.platform == item.platform:
            source.metadata_json = {**source.metadata_json, **item.metadata, "discovery_rank": ranking}
    return created


def schedule_discovery(db, timestamp):
    for row in db.scalars(select(DiscoverySchedule).where(
            DiscoverySchedule.enabled.is_(True), DiscoverySchedule.next_run_at <= timestamp)):
        queued = db.scalar(select(Job.id).where(Job.kind == "discovery", Job.status.in_(ACTIVE),
                                               Job.payload["schedule_id"].as_integer() == row.id).limit(1))
        if queued:
            continue
        window = utc(row.next_run_at)
        enqueue(db, "discovery", key=f"discovery:{row.id}:{window.isoformat()}",
                payload={"schedule_id": row.id})
        interval = timedelta(minutes=row.interval_minutes)
        row.next_run_at = window + interval * (1 + (timestamp - window) // interval)


def schedule_processing(db, policy, timestamp):
    count = db.scalar(select(func.count()).select_from(Job).where(Job.kind.in_(("process_source", "render_clip", "quality_check")),
                                                                Job.status.in_(ACTIVE)))
    capacity = policy.max_concurrent_processing_jobs - count if policy.auto_process_authorized else 0
    budget_open = can_afford(policy, budget_used(db, timestamp), int(settings.processing_cents_per_minute > 0))
    sources = db.scalars(select(Source).where(Source.status.in_(
        ("discovered", "rights_review", "awaiting_authorized_media", "authorized"),
    )).order_by(Source.id))
    for source in sources:
        rights = latest_rights(db, source.id)
        if not rights or not can_ingest_and_publish(rights, timestamp):
            continue
        if not authorized_media(source):
            source.status = "awaiting_authorized_media"
            continue
        if capacity <= 0 or not budget_open:
            continue
        lock_source(db, source.id)
        db.refresh(source)
        rights = latest_rights(db, source.id)
        if (not rights or not can_ingest_and_publish(rights, timestamp)
                or not authorized_media(source) or source.status == "analyzed"):
            continue
        key = sha256(f"auto-process:{source.id}:{source.media_revision}".encode()).hexdigest()
        if db.scalar(select(Job.id).where(Job.source_id == source.id, Job.kind == "process_source",
                                       (Job.status.in_(ACTIVE)) | (Job.idempotency_key == key)).limit(1)):
            continue
        enqueue_processing(db, source, idempotency_key=key, automated=True)
        capacity -= 1


def schedule_publications(db, policy: Policy, timestamp):
    if not policy.auto_publish or not policy.in_window(timestamp):
        return
    active = db.scalar(select(func.count()).select_from(Job).where(Job.kind == "publish", Job.status.in_(ACTIVE)))
    capacity = policy.max_concurrent_publish_jobs - active
    for platform, accounts in policy.target_accounts.items():
        if not settings.publishing_allowed(platform):
            continue
        limit = policy.daily_publish_limits.get(platform, 0)
        available = limit - platform_quota_used(db, platform, policy, timestamp)
        if min(capacity, available) <= 0 or not accounts:
            continue
        if platform == "instagram" and not policy.instagram_visibility_acknowledged:
            continue
        signal = db.scalar(select(Job.result).where(Job.kind == "optimize", Job.status == "succeeded",
                 Job.payload["platform"].as_string() == platform).order_by(Job.created_at.desc()).limit(1)) or {}
        for account_id in accounts:
            account = db.get(ConnectedAccount, account_id)
            if not account or account.status != "connected" or account.platform != platform or not account.access_token:
                continue
            clips = list(db.scalars(select(Clip).where(Clip.status == "approved",
                ~exists().where(Publication.clip_id == Clip.id, Publication.platform == platform,
                                Publication.account_id == account_id),
            ).order_by(Clip.score.desc(), Clip.id)))
            preferred = signal.get("preferred_duration_band")
            if preferred:
                clips.sort(key=lambda clip: -(clip.score + 5 * (duration_band(clip.end_seconds - clip.start_seconds) == preferred)))
            for clip in clips:
                if min(capacity, available) <= 0:
                    return
                rights = latest_rights(db, clip.source_id)
                if not rights or not can_publish_to(rights, platform, timestamp):
                    continue
                payload = PublicationRequest(platform=platform, account_id=account_id, scheduled_at=timestamp,
                    privacy_status=policy.default_privacy if platform == "youtube" else "private",
                    instagram_visibility_acknowledged=policy.instagram_visibility_acknowledged)
                try:
                    schedule_publication(db, clip.id, payload, "scheduler", automated=True)
                except HTTPException as exc:
                    logger.warning("automatic_publication_not_queued", extra={"clip_id": clip.id, "status": exc.status_code})
                    continue
                available -= 1
                capacity -= 1
                if available <= 0:
                    break
            if min(capacity, available) <= 0:
                break


def schedule_analytics(db, timestamp):
    """Collect each publication once per due window (initial, 1h, 24h, 72h, 7d, 28d after publish)."""
    rows = db.scalars(select(Publication).where(Publication.status == "succeeded", Publication.published_at.is_not(None)))
    for row in rows:
        window = analytics_jobs.due_window(row.published_at, timestamp)
        if window is None or db.scalar(select(AnalyticsCollection.id).where(
                AnalyticsCollection.publication_id == row.id, AnalyticsCollection.window == window)):
            continue
        pending = db.scalar(select(Job.id).where(Job.kind == "analytics", Job.publication_id == row.id,
                                                  Job.status.in_(ACTIVE)).limit(1))
        if not pending:
            enqueue(db, "analytics", key=f"analytics:{row.id}:{window}", publication_id=row.id,
                    payload={"window": window})


def scheduler_once(session_factory, *, timestamp=None) -> None:
    with session_factory() as db:
        row = lock_policy(db)
        if row.emergency_stopped:
            return
        timestamp = utc(timestamp) if timestamp is not None else now()
        policy = Policy.model_validate(row.configuration)
        schedule_discovery(db, timestamp)
        schedule_processing(db, policy, timestamp)
        schedule_publications(db, policy, timestamp)
        schedule_analytics(db, timestamp)
        db.commit()
