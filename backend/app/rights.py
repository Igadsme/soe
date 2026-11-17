from datetime import datetime, timezone

from .models import RightsRecord


def can_ingest_and_publish(record: RightsRecord, at: datetime | None = None) -> bool:
    """Deterministic gate: unresolved, revoked, expired, or non-commercial rights fail closed."""
    at = at or datetime.now(timezone.utc)
    if not record.allowed_platforms or not set(record.allowed_platforms) <= {"upload", "youtube", "twitch", "tiktok", "instagram"} or record.revoked or not record.commercial_use or not record.publishing_allowed:
        return False
    return record.expires_at is None or record.expires_at.replace(tzinfo=record.expires_at.tzinfo or timezone.utc) > at


def can_publish_to(record: RightsRecord, platform: str, at: datetime | None = None) -> bool:
    return can_ingest_and_publish(record, at) and platform in record.allowed_platforms
