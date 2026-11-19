"""Automation policy and serialized queue admission."""
import random
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .config import settings
from .models import (
    AutomationPolicy,
    DiscoverySchedule,
    Job,
    LedgerEntry,
    Publication,
    WorkflowRun,
)
from .publishing import Platform, utc

ACTIVE = ("queued", "running", "cancel_requested")
TERMINAL = ("dead_letter", "failed", "cancelled")
PROCESSING_KINDS = ("process_source", "quality_check")
PROCESSING_CATEGORY = "processing_estimate"


class PublishingWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=0, le=24)

    @model_validator(mode="after")
    def check_range(self):
        if self.start_hour == self.end_hour:
            raise ValueError("window must have positive length")
        return self

    def contains(self, hour: int) -> bool:
        if self.start_hour < self.end_hour:
            return self.start_hour <= hour < self.end_hour
        return hour >= self.start_hour or hour < self.end_hour


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto_process_authorized: bool = False
    auto_publish: bool = False
    approval_mode: Literal["manual", "auto_after_qc"] = "manual"
    min_clip_score: float = Field(default=70, ge=0, le=100)
    max_clips_per_source: int = Field(default=5, ge=1, le=25)
    daily_publish_limits: dict[Platform, Annotated[int, Field(ge=0, le=1000)]] = Field(default_factory=dict)
    target_accounts: dict[Platform, list[Annotated[int, Field(gt=0, le=2147483647)]]] = Field(default_factory=dict)
    publishing_windows: list[PublishingWindow] = Field(default_factory=list, max_length=24)
    timezone: str = Field(default="UTC", max_length=100)
    default_privacy: Literal["private", "unlisted"] = "private"
    instagram_visibility_acknowledged: bool = False
    daily_budget_cents: int | None = Field(default=None, ge=0, le=2147483647)
    monthly_budget_cents: int | None = Field(default=None, ge=0, le=2147483647)
    max_concurrent_processing_jobs: int = Field(default=1, ge=1, le=32)
    max_concurrent_publish_jobs: int = Field(default=1, ge=1, le=32)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown IANA timezone") from None
        return value

    @field_validator("target_accounts")
    @classmethod
    def distinct_accounts(cls, value):
        if any(len(ids) > 20 or len(set(ids)) != len(ids) for ids in value.values()):
            raise ValueError("provide up to 20 distinct accounts per platform")
        return value

    def in_window(self, timestamp: datetime) -> bool:
        hour = utc(timestamp).astimezone(ZoneInfo(self.timezone)).hour
        return any(window.contains(hour) for window in self.publishing_windows)


Channel = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")]


class ScheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    platform: Literal["youtube", "twitch"]
    query: str = Field(min_length=1, max_length=200)
    channel_allowlist: list[Channel] = Field(default_factory=list, max_length=10)
    interval_minutes: int = Field(default=60, ge=1, le=43200)
    enabled: bool = True
    max_results: int = Field(default=10, ge=1, le=50)

    @field_validator("channel_allowlist")
    @classmethod
    def distinct_channels(cls, value):
        if len({channel.casefold() for channel in value}) != len(value):
            raise ValueError("channel allowlist must contain distinct entries")
        return value


class ScheduleOutput(ScheduleInput):
    model_config = ConfigDict(from_attributes=True)
    id: int
    last_run_at: datetime | None
    next_run_at: datetime
    last_status: str | None


SCHEDULE_FIELDS = tuple(ScheduleInput.model_fields)


def lock_policy(db) -> AutomationPolicy:
    """Take the singleton write lock before admission, stop or quota reservation."""
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    # Migrated databases have this row. Bootstrap also supports metadata.create_all.
    db.execute(insert(AutomationPolicy).values(id=1, configuration={}, emergency_stopped=False)
               .on_conflict_do_nothing(index_elements=["id"]))
    db.execute(update(AutomationPolicy).where(AutomationPolicy.id == 1)
               .values(emergency_stopped=AutomationPolicy.emergency_stopped))
    return db.get(AutomationPolicy, 1, populate_existing=True)


def get_policy(db) -> Policy:
    row = db.get(AutomationPolicy, 1)
    return Policy.model_validate(row.configuration if row else {})


def stopped(db) -> bool:
    row = db.get(AutomationPolicy, 1)
    return bool(row and row.emergency_stopped)


def budget_used(db, timestamp: datetime) -> tuple[int, int]:
    day = utc(timestamp).replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    return tuple(db.scalar(select(func.coalesce(func.sum(LedgerEntry.cents), 0)).where(
        LedgerEntry.category == PROCESSING_CATEGORY, LedgerEntry.kind == "expense",
        LedgerEntry.created_at >= boundary,
    )) for boundary in (day, month))


def can_afford(policy: Policy, used: tuple[int, int], estimate: int) -> bool:
    return all(cap is None or total + estimate <= cap for cap, total in zip(
        (policy.daily_budget_cents, policy.monthly_budget_cents), used, strict=True))


def platform_quota_used(db, platform: str, policy: Policy, timestamp: datetime) -> int:
    zone = ZoneInfo(policy.timezone)
    start = utc(timestamp).astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    return db.scalar(select(func.count()).select_from(Publication).where(
        Publication.platform == platform,
        or_(Publication.published_at >= utc(start),
            Publication.published_at.is_(None) & (Publication.upload_started_at >= utc(start))),
    ))


def retry_delay(attempts: int) -> float:
    return min(300, 2 ** min(attempts, 9)) + random.uniform(0, 2)  # noqa: S311


def job_limits(kind: str) -> tuple[int, float]:
    return {
        "process_source": (3, settings.processing_deadline_floor_seconds),
        "publish": (settings.publishing_max_attempts, settings.publishing_deadline_seconds),
        "quality_check": (3, 300),
        "discovery": (3, 300),
        "analytics": (3, 60),
        "optimize": (2, 30),
    }[kind]


def update_workflow(db, job: Job, state: str, error: str | None = None) -> None:
    if job.workflow_id:
        db.execute(update(WorkflowRun).where(WorkflowRun.id == job.workflow_id).values(state=state, error=error))


def record_schedule_status(db, job: Job, status: str) -> None:
    schedule = db.get(DiscoverySchedule, (job.payload or {}).get("schedule_id", 0))
    if schedule and (schedule.last_run_at is None or utc(schedule.last_run_at) <= utc(job.created_at)):
        schedule.last_run_at = job.created_at
        schedule.last_status = status
