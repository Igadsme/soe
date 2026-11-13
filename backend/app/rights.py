from datetime import datetime, timezone
from .models import RightsRecord


def can_ingest_and_publish(record: RightsRecord, at: datetime | None = None) -> bool:
    """Deterministic gate: unresolved, revoked, expired, or non-commercial rights fail closed."""
    at = at or datetime.now(timezone.utc)
    if record.revoked or not record.commercial_use or not record.publishing_allowed:
        return False
    return record.expires_at is None or record.expires_at > at
