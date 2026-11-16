from datetime import datetime, timedelta, timezone


def retry_delay(attempt: int, base_seconds: int = 30, max_seconds: int = 3600) -> int:
    return min(max_seconds, base_seconds * (2 ** max(0, attempt - 1)))


def can_schedule(published_today: int, daily_limit: int, scheduled_for: datetime | None = None) -> bool:
    if published_today >= daily_limit:
        return False
    if scheduled_for is None:
        return True
    return scheduled_for >= datetime.now(timezone.utc)


def next_attempt(attempt: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) + timedelta(seconds=retry_delay(attempt))
