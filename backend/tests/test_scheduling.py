from datetime import datetime, timedelta, timezone
from app.scheduling import can_schedule, next_attempt, retry_delay


def test_retry_and_daily_limit_are_deterministic():
    assert retry_delay(1) == 30
    assert retry_delay(3) == 120
    assert retry_delay(99) == 3600
    assert can_schedule(0, 1)
    assert not can_schedule(1, 1)
    assert not can_schedule(0, 1, datetime.now(timezone.utc) - timedelta(seconds=1))
    assert next_attempt(2) > datetime.now(timezone.utc)
