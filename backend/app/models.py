from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


class Source(Base):
    __tablename__ = "sources"

    @property
    def has_media(self) -> bool:
        return bool(self.media_path)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(32), default="discovered")
    media_path: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    media_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    media_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RightsRecord(Base):
    __tablename__ = "rights_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    authorization_type: Mapped[str] = mapped_column(String(64))
    commercial_use: Mapped[bool] = mapped_column(Boolean, default=False)
    publishing_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence: Mapped[str] = mapped_column(Text)
    allowed_platforms: Mapped[list] = mapped_column(JSON, default=list)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Clip(Base):
    __tablename__ = "clips"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    start_seconds: Mapped[float] = mapped_column(Float)
    end_seconds: Mapped[float] = mapped_column(Float)
    description: Mapped[str] = mapped_column(Text)
    hook: Mapped[str] = mapped_column(String(500))
    score: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default="candidate")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    output_path: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    subtitle_path: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    state: Mapped[str] = mapped_column(String(64), default="queued")
    cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ConnectedAccount(Base):
    __tablename__ = "connected_accounts"
    __table_args__ = (UniqueConstraint("platform", "external_account_id", name="uq_account_platform_external"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    external_account_id: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(500))
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    access_token: Mapped[str | None] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="connected")
    daily_upload_limit: Mapped[int] = mapped_column(Integer, default=5)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class OAuthState(Base):
    __tablename__ = "oauth_states"
    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_binding: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(String(32))
    code_verifier: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Publication(Base):
    __tablename__ = "publications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clip_id: Mapped[int] = mapped_column(ForeignKey("clips.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("connected_accounts.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    automated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    media_sha256: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(100), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    privacy_status: Mapped[str] = mapped_column(String(32), default="private")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bytes_sent: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_total: Mapped[int] = mapped_column(BigInteger, default=0)
    upload_checkpoint: Mapped[str | None] = mapped_column(Text)
    upload_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_id: Mapped[str | None] = mapped_column(String(255))
    external_url: Mapped[str | None] = mapped_column(String(1000))
    error: Mapped[str | None] = mapped_column(Text)
    error_class: Mapped[str | None] = mapped_column(String(32))
    last_error: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class PublicationEvent(Base):
    __tablename__ = "publication_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[int] = mapped_column(ForeignKey("publications.id", ondelete="CASCADE"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(255))
    error_class: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MetricEvent(Base):
    __tablename__ = "metric_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[int] = mapped_column(Integer, index=True)
    metric: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # source: which API/flow produced it; provenance: verified (official API) | imported | manual.
    source: Mapped[str] = mapped_column(String(32), default="api")
    provenance: Mapped[str] = mapped_column(String(16), default="verified")
    window: Mapped[str | None] = mapped_column(String(16), nullable=True)


class AnalyticsCollection(Base):
    """One collection attempt per publication and window; records 'unavailable' instead of inventing zeros."""
    __tablename__ = "analytics_collections"
    __table_args__ = (UniqueConstraint("publication_id", "window", name="uq_analytics_collection_window"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[int] = mapped_column(Integer, index=True)
    window: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(64))
    cents: Mapped[int] = mapped_column(Integer)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # verified_platform | imported | manual | estimated (estimated is only ever a cost).
    provenance: Mapped[str] = mapped_column(String(24), default="manual")
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    platform: Mapped[str | None] = mapped_column(String(16), nullable=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publication_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    import_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)


class RevenueImport(Base):
    __tablename__ = "revenue_imports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    filename: Mapped[str] = mapped_column(String(255))
    rows: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[int] = mapped_column(Integer, index=True)
    last_seen_at: Mapped[int] = mapped_column(Integer, default=lambda: int(__import__("time").time()))
    password_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    revoked_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Job(Base):
    __tablename__ = "jobs"
    _active = "status IN ('queued', 'running', 'cancel_requested')"
    _active_source = f"kind = 'process_source' AND {_active}"
    __table_args__ = (
        Index("uq_jobs_active_source", "source_id", unique=True,
              sqlite_where=text(_active_source), postgresql_where=text(_active_source)),
        Index("uq_jobs_active_publication", "publication_id", unique=True,
              sqlite_where=text(f"kind = 'publish' AND {_active}"),
              postgresql_where=text(f"kind = 'publish' AND {_active}")),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), default="process_source")
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"))
    workflow_id: Mapped[int | None] = mapped_column(ForeignKey("workflow_runs.id"))
    publication_id: Mapped[int | None] = mapped_column(ForeignKey("publications.id", ondelete="CASCADE"))
    replay_of_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), unique=True)
    payload: Mapped[dict | None] = mapped_column(JSON)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_class: Mapped[str | None] = mapped_column(String(32))
    wait_reason: Mapped[str | None] = mapped_column(String(32))
    interruption: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

class AutomationPolicy(Base):
    __tablename__ = "automation_policy"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)
    emergency_stopped: Mapped[bool] = mapped_column(Boolean, default=False)
    worker_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DiscoverySchedule(Base):
    __tablename__ = "discovery_schedules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(16))
    query: Mapped[str] = mapped_column(String(200))
    channel_allowlist: Mapped[list[str]] = mapped_column(JSON, default=list)
    interval_minutes: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_results: Mapped[int] = mapped_column(Integer)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_status: Mapped[str | None] = mapped_column(String(64))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(32), index=True)
    entity_id: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
