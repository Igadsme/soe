from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from app.rights import can_ingest_and_publish, can_publish_to


def test_rights_fail_closed_and_scope_platforms():
    base = dict(revoked=False, commercial_use=True, publishing_allowed=True, expires_at=None, allowed_platforms=["youtube"])
    assert can_ingest_and_publish(SimpleNamespace(**base))
    assert can_publish_to(SimpleNamespace(**base), "youtube")
    assert not can_publish_to(SimpleNamespace(**base), "tiktok")
    assert not can_ingest_and_publish(SimpleNamespace(**{**base, "commercial_use": False}))
    assert not can_ingest_and_publish(SimpleNamespace(**{**base, "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}))
