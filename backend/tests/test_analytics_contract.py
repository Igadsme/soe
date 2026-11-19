"""Analytics adapters (contract tests with httpx.MockTransport; not live API passes) and window idempotency."""
import json
from datetime import timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from app import analytics_jobs, publishing, worker
from app.config import settings
from app.models import AnalyticsCollection, Clip, ConnectedAccount, Job, MetricEvent, Publication, Source, now
from app.token_vault import token_vault


def install(monkeypatch, handler):
    seen = []

    async def wrapped(request):
        await request.aread()
        seen.append(request)
        return handler(request)
    monkeypatch.setattr(publishing, "provider_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(wrapped)))
    return seen


def account(monkeypatch, platform, scopes=()):
    monkeypatch.setattr(settings, "token_encryption_keys", Fernet.generate_key().decode())
    return ConnectedAccount(platform=platform, external_account_id="acct", display_name="Account", status="connected",
                            scopes=list(scopes), access_token=token_vault().encrypt(b"fixture-token").decode())


def publication(platform, external_id, published_at=None):
    return Publication(id=1, clip_id=1, platform=platform, status="succeeded", external_id=external_id,
                       published_at=published_at or now() - timedelta(days=8))


@pytest.mark.asyncio
async def test_youtube_statistics_and_analytics_report(monkeypatch):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer fixture-token"
        if request.url.host == "www.googleapis.com":
            assert dict(request.url.params) == {"part": "statistics", "id": "vid123"}
            return httpx.Response(200, json={"items": [{"id": "vid123", "statistics": {"viewCount": "42", "likeCount": "3", "commentCount": "1"}}]})
        assert request.url.host == "youtubeanalytics.googleapis.com" and request.url.path == "/v2/reports"
        params = dict(request.url.params)
        assert params["ids"] == "channel==MINE" and params["dimensions"] == "video" and params["filters"] == "video==vid123"
        assert "averageViewPercentage" in params["metrics"] and "estimatedRevenue" not in params["metrics"]
        return httpx.Response(200, json={"columnHeaders": [{"name": "video"}, {"name": "views"}, {"name": "averageViewPercentage"}],
                                         "rows": [["vid123", 40, 55.5]]})
    seen = install(monkeypatch, handler)
    metrics = await analytics_jobs.YouTubeStatistics().collect(
        publication("youtube", "vid123"), account(monkeypatch, "youtube", [analytics_jobs.YOUTUBE_ANALYTICS_SCOPE]))
    assert metrics == {"views": 40.0, "likes": 3, "comments": 1, "average_view_percentage": 55.5}
    assert len(seen) == 2
    seen.clear()
    metrics = await analytics_jobs.YouTubeStatistics().collect(publication("youtube", "vid123"), account(monkeypatch, "youtube"))
    assert metrics == {"views": 42, "likes": 3, "comments": 1} and len(seen) == 1  # no analytics scope → no report call


@pytest.mark.asyncio
async def test_tiktok_video_query_contract(monkeypatch):
    def handler(request):
        assert request.method == "POST" and request.url.path == "/v2/video/query/"
        assert request.url.params["fields"] == "id,view_count,like_count,comment_count,share_count"
        assert json.loads(request.content) == {"filters": {"video_ids": ["7300000000000000001"]}}
        return httpx.Response(200, json={"data": {"videos": [{"id": "7300000000000000001", "view_count": 9, "like_count": 2,
                                                              "comment_count": 0, "share_count": 1}]}, "error": {"code": "ok"}})
    install(monkeypatch, handler)
    metrics = await analytics_jobs.TikTokVideoQuery().collect(publication("tiktok", "7300000000000000001"),
                                                              account(monkeypatch, "tiktok", ["video.list"]))
    assert metrics == {"views": 9, "likes": 2, "comments": 0, "shares": 1}
    with pytest.raises(analytics_jobs.Unavailable, match="public post id"):
        await analytics_jobs.TikTokVideoQuery().collect(publication("tiktok", "v_pub_file~v2.1"), account(monkeypatch, "tiktok", ["video.list"]))
    with pytest.raises(analytics_jobs.Unavailable, match="video.list"):
        await analytics_jobs.TikTokVideoQuery().collect(publication("tiktok", "7300000000000000001"), account(monkeypatch, "tiktok"))


@pytest.mark.asyncio
async def test_instagram_insights_with_deprecated_metric_fallback(monkeypatch):
    calls = []

    def handler(request):
        assert request.headers["Authorization"] == "Bearer fixture-token" and "access_token" not in request.url.params
        assert request.url.path.endswith("/17890000000000001/insights")
        calls.append(request.url.params["metric"])
        if len(calls) == 1:
            return httpx.Response(400, json={"error": {"code": 100, "message": "metric views is not supported"}})
        return httpx.Response(200, json={"data": [{"name": "reach", "values": [{"value": 30}]},
                                                  {"name": "likes", "total_value": {"value": 4}}]})
    install(monkeypatch, handler)
    metrics = await analytics_jobs.InstagramInsights().collect(publication("instagram", "17890000000000001"), account(monkeypatch, "instagram"))
    assert metrics == {"reach": 30.0, "likes": 4.0}
    assert calls == [",".join(analytics_jobs.INSTAGRAM_METRICS), ",".join(analytics_jobs.INSTAGRAM_FALLBACK_METRICS)]


@pytest.mark.asyncio
async def test_provider_failures_become_unavailable_or_retry(monkeypatch):
    install(monkeypatch, lambda request: httpx.Response(403, json={"error": {"code": 10}}))
    with pytest.raises(analytics_jobs.Unavailable, match="permission"):
        await analytics_jobs.InstagramInsights().collect(publication("instagram", "1789"), account(monkeypatch, "instagram"))
    install(monkeypatch, lambda request: httpx.Response(503))
    with pytest.raises(publishing.PublishingUnavailable):
        await analytics_jobs.YouTubeStatistics().collect(publication("youtube", "vid"), account(monkeypatch, "youtube"))
    with pytest.raises(analytics_jobs.Unavailable, match="no connected account"):
        await analytics_jobs.YouTubeStatistics().collect(publication("youtube", "vid"), None)
    expired = account(monkeypatch, "tiktok", ["video.list"])
    expired.expires_at = now() - timedelta(minutes=1)
    with pytest.raises(analytics_jobs.Unavailable, match="expired"):
        await analytics_jobs.TikTokVideoQuery().collect(publication("tiktok", "73"), expired)


def test_due_windows():
    published = now()
    assert analytics_jobs.due_window(published, published) == "initial"
    assert analytics_jobs.due_window(published, published + timedelta(hours=2)) == "1h"
    assert analytics_jobs.due_window(published, published + timedelta(days=7, minutes=1)) == "7d"
    assert analytics_jobs.due_window(published, published + timedelta(days=90)) == "28d"
    assert analytics_jobs.due_window(published, published - timedelta(seconds=5)) is None


@pytest.mark.asyncio
async def test_collection_is_idempotent_per_window_and_records_unavailable(isolated_database, monkeypatch):
    with isolated_database() as db:
        source = Source(platform="upload", external_id="an", title="t", url="https://example.test/a")
        db.add(source)
        db.flush()
        clip = Clip(source_id=source.id, start_seconds=0, end_seconds=10, description="d", hook="h", score=80, status="approved", evidence={})
        db.add(clip)
        db.flush()
        db.add_all([Publication(clip_id=clip.id, platform="tiktok", status="succeeded", external_id="v_pub_x",
                                published_at=now() - timedelta(hours=2)),
                    Publication(clip_id=clip.id, platform="youtube", status="succeeded", external_id="vid9",
                                published_at=now() - timedelta(hours=2))])
        db.commit()

    class Fixed:
        provider = "youtube"

        async def collect(self, publication, account):
            return {"views": 5}
    monkeypatch.setitem(analytics_jobs.ADAPTERS, "youtube", Fixed())
    for _ in range(3):
        worker.scheduler.scheduler_once(isolated_database)
        while await worker.process_once():
            pass
    with isolated_database() as db:
        rows = {(row.provider, row.window): (row.status, row.reason) for row in db.scalars(select(AnalyticsCollection))}
        assert rows[("youtube", "1h")] == ("collected", None)
        assert rows[("tiktok", "1h")][0] == "unavailable" and "public post id" in rows[("tiktok", "1h")][1]
        assert len(rows) == 2
        events = list(db.scalars(select(MetricEvent)))
        assert [(e.metric, e.value, e.window, e.provenance, e.source) for e in events] == [("views", 5, "1h", "verified", "youtube_api")]
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == "analytics")) == 2
