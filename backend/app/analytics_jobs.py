"""Periodic provider statistics and evidence-backed scheduling signals."""
from statistics import median
from typing import Protocol

from cryptography.fernet import InvalidToken
from sqlalchemy import func, select

from . import oauth, publishing
from .models import Clip, ConnectedAccount, MetricEvent, Publication, now
from .token_vault import token_vault


class AnalyticsAdapter(Protocol):
    async def collect(self, publication: Publication, account: ConnectedAccount | None) -> dict[str, int] | None: ...


class YouTubeStatistics:
    async def collect(self, publication, account) -> dict[str, int] | None:
        if not publication.external_id or not account or account.status != "connected" or not account.access_token:
            return None
        async with publishing.provider_client() as client:
            try:
                if account.expires_at and publishing.utc(account.expires_at) <= now():
                    # Read-only Google access. The upload worker owns durable token rotation.
                    token = (await oauth.refresh(client, account)).access
                else:
                    token = token_vault().decrypt(account.access_token.encode()).decode()
                response = await publishing.send(client, "GET", "https://www.googleapis.com/youtube/v3/videos",
                    headers=publishing.bearer(token),
                    params={"part": "statistics", "id": publication.external_id})
                publishing.provider_error(response)
                items = publishing.response_data(response).get("items", [])
                data = next((item["statistics"] for item in items if item.get("id") == publication.external_id), {})
                return {metric: int(data[key]) for key, metric in YOUTUBE_METRICS.items() if key in data} or None
            except (InvalidToken, ValueError, KeyError, TypeError):
                return None
            except publishing.PublishingUnavailable as exc:
                if exc.error_class in publishing.RETRYABLE:
                    raise
                return None


YOUTUBE_METRICS = {"viewCount": "views", "likeCount": "likes", "commentCount": "comments"}
ADAPTERS: dict[str, AnalyticsAdapter] = {"youtube": YouTubeStatistics()}


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
