"""Periodic provider statistics and evidence-backed scheduling signals."""
from statistics import median
from typing import Protocol

from cryptography.fernet import InvalidToken
from sqlalchemy import func, select

from . import oauth, publishing
from .models import Clip, ConnectedAccount, MetricEvent, Publication, now
from .token_vault import token_vault


class Unavailable(Exception):
    """Metrics cannot be collected; the reason is stored so the UI never shows a fabricated zero."""


class AnalyticsAdapter(Protocol):
    provider: str

    async def collect(self, publication: Publication, account: ConnectedAccount | None) -> dict[str, float]: ...


WINDOWS: tuple[tuple[str, int], ...] = (("initial", 0), ("1h", 3600), ("24h", 86400), ("72h", 259200),
                                        ("7d", 604800), ("28d", 2419200))


def due_window(published_at, timestamp) -> str | None:
    """Latest collection window whose offset has elapsed since publication."""
    age = (publishing.utc(timestamp) - publishing.utc(published_at)).total_seconds()
    due = [name for name, offset in WINDOWS if age >= offset]
    return due[-1] if due else None


async def access_token(client, account, *, refresh_allowed: bool) -> str:
    if not account or account.status != "connected" or not account.access_token:
        raise Unavailable("no connected account with a stored token")
    try:
        if account.expires_at and publishing.utc(account.expires_at) <= now():
            if not refresh_allowed:
                raise Unavailable("access token expired; it is refreshed by the next publish")
            # Read-only Google access. The upload worker owns durable token rotation.
            return (await oauth.refresh(client, account)).access
        return token_vault().decrypt(account.access_token.encode()).decode()
    except (InvalidToken, ValueError):
        raise Unavailable("stored token could not be decrypted") from None


def guard(exc: publishing.PublishingUnavailable):
    if exc.error_class in publishing.RETRYABLE:
        raise exc
    raise Unavailable(f"provider refused: {exc.error_class}") from None


class YouTubeStatistics:
    """Data API v3 videos.list (part=statistics) plus optional YouTube Analytics API v2 reports."""
    provider = "youtube"

    async def collect(self, publication, account) -> dict[str, float]:
        if not publication.external_id:
            raise Unavailable("publication has no YouTube video id")
        async with publishing.provider_client() as client:
            token = await access_token(client, account, refresh_allowed=True)
            try:
                response = await publishing.send(client, "GET", "https://www.googleapis.com/youtube/v3/videos",
                    headers=publishing.bearer(token), params={"part": "statistics", "id": publication.external_id})
                publishing.provider_error(response)
                items = publishing.response_data(response).get("items", [])
                data = next((item["statistics"] for item in items if item.get("id") == publication.external_id), {})
                metrics = {metric: int(data[key]) for key, metric in YOUTUBE_METRICS.items() if key in data}
                if YOUTUBE_ANALYTICS_SCOPE in (account.scopes or []) and publication.published_at:
                    metrics.update(await self.report(client, token, publication))
            except publishing.PublishingUnavailable as exc:
                guard(exc)
            except (KeyError, TypeError, ValueError):
                raise Unavailable("unexpected YouTube response shape") from None
        if not metrics:
            raise Unavailable("YouTube returned no statistics for this video")
        return metrics

    async def report(self, client, token, publication) -> dict[str, float]:
        params = {"ids": "channel==MINE", "metrics": ",".join(YOUTUBE_REPORT_METRICS), "dimensions": "video",
                  "filters": f"video=={publication.external_id}",
                  "startDate": publishing.utc(publication.published_at).date().isoformat(), "endDate": now().date().isoformat()}
        response = await publishing.send(client, "GET", "https://youtubeanalytics.googleapis.com/v2/reports",
                                         headers=publishing.bearer(token), params=params)
        publishing.provider_error(response)
        body = publishing.response_data(response)
        headers = [column.get("name") for column in body.get("columnHeaders", [])]
        rows = body.get("rows") or []
        if not rows:
            return {}
        return {YOUTUBE_REPORT_METRICS[name]: float(value) for name, value in zip(headers, rows[0], strict=False)
                if name in YOUTUBE_REPORT_METRICS}


class TikTokVideoQuery:
    """Display API /v2/video/query/ (scope video.list); needs the public post id, not a publish_id."""
    provider = "tiktok"

    async def collect(self, publication, account) -> dict[str, float]:
        video_id = publication.external_id or ""
        if not video_id.isdigit():
            raise Unavailable("TikTok has not returned a public post id (private/SELF_ONLY posts have none)")
        if "video.list" not in (account.scopes if account else []):
            raise Unavailable("TikTok account lacks the video.list scope")
        async with publishing.provider_client() as client:
            token = await access_token(client, account, refresh_allowed=False)
            try:
                response = await publishing.send(client, "POST", "https://open.tiktokapis.com/v2/video/query/",
                    headers={**publishing.bearer(token), "Content-Type": "application/json"},
                    params={"fields": "id,view_count,like_count,comment_count,share_count"},
                    json={"filters": {"video_ids": [video_id]}})
                publishing.provider_error(response)
            except publishing.PublishingUnavailable as exc:
                guard(exc)
        videos = (publishing.response_data(response).get("data") or {}).get("videos") or []
        data = next((video for video in videos if str(video.get("id")) == video_id), None)
        if not data:
            raise Unavailable("TikTok returned no data for this post")
        return {metric: float(data[key]) for key, metric in TIKTOK_METRICS.items() if isinstance(data.get(key), (int, float))}


class InstagramInsights:
    """Graph API GET /{ig-media-id}/insights; falls back to a smaller metric set if a metric is deprecated."""
    provider = "instagram"

    async def collect(self, publication, account) -> dict[str, float]:
        if not publication.external_id:
            raise Unavailable("publication has no Instagram media id")
        media_id = publishing.require_id(publication.external_id)
        async with publishing.provider_client() as client:
            token = await access_token(client, account, refresh_allowed=False)
            response = None
            for metrics in (INSTAGRAM_METRICS, INSTAGRAM_FALLBACK_METRICS):
                response = await publishing.send(client, "GET", f"{publishing.graph_url()}/{media_id}/insights",
                    headers=publishing.bearer(token), params={"metric": ",".join(metrics)})
                error = publishing.response_data(response).get("error") or {}
                if response.status_code == 400 and isinstance(error, dict) and error.get("code") == 100:
                    continue  # unsupported/deprecated metric for this media type: retry with the core set
                break
            try:
                publishing.provider_error(response)
            except publishing.PublishingUnavailable as exc:
                guard(exc)
        result: dict[str, float] = {}
        for item in publishing.response_data(response).get("data", []):
            value = (item.get("total_value") or {}).get("value")
            if value is None and item.get("values"):
                value = item["values"][0].get("value")
            if isinstance(value, (int, float)) and item.get("name"):
                result[item["name"]] = float(value)
        if not result:
            raise Unavailable("Instagram returned no insights for this media")
        return result


YOUTUBE_METRICS = {"viewCount": "views", "likeCount": "likes", "commentCount": "comments"}
YOUTUBE_ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
YOUTUBE_REPORT_METRICS = {"views": "views", "estimatedMinutesWatched": "estimated_minutes_watched",
                          "averageViewDuration": "average_view_duration_seconds", "averageViewPercentage": "average_view_percentage",
                          "likes": "likes", "comments": "comments", "shares": "shares",
                          "subscribersGained": "subscribers_gained", "subscribersLost": "subscribers_lost"}
TIKTOK_METRICS = {"view_count": "views", "like_count": "likes", "comment_count": "comments", "share_count": "shares"}
INSTAGRAM_METRICS = ("views", "reach", "likes", "comments", "shares", "saved", "total_interactions")
INSTAGRAM_FALLBACK_METRICS = ("reach", "likes", "comments", "shares", "saved")
ADAPTERS: dict[str, AnalyticsAdapter] = {"youtube": YouTubeStatistics(), "tiktok": TikTokVideoQuery(),
                                         "instagram": InstagramInsights()}


def duration_band(seconds: float) -> str:
    return "under_30_seconds" if seconds < 30 else "30_seconds_or_longer"


def optimize(db, platform: str) -> dict:
    # Use the first measured snapshot per publication to reduce age bias.
    first = (select(MetricEvent.publication_id, func.min(MetricEvent.id).label("metric_id"))
             .where(MetricEvent.verified.is_(True), MetricEvent.metric == "views")
             .group_by(MetricEvent.publication_id).subquery())
    rows = db.execute(select(Clip.end_seconds - Clip.start_seconds, MetricEvent.value)
        .join(Publication, Publication.clip_id == Clip.id)
        .join(first, first.c.publication_id == Publication.id)
        .join(MetricEvent, MetricEvent.id == first.c.metric_id)
        .where(Publication.platform == platform, Publication.status == "succeeded")
        .order_by(Publication.published_at.desc()).limit(100))
    samples: dict[str, list[float]] = {}
    for seconds, views in rows:
        samples.setdefault(duration_band(seconds), []).append(views)
    signals = {band: {"samples": len(views), "median_views": median(views)}
               for band, views in samples.items() if len(views) >= 3}
    preferred = None
    if len(signals) == 2:
        best, other = sorted(signals, key=lambda band: signals[band]["median_views"], reverse=True)
        if signals[best]["median_views"] > signals[other]["median_views"]:
            preferred = best
    return {"platform": platform, "observations": signals, "preferred_duration_band": preferred}
