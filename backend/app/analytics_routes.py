"""Analytics read models: collection status per publication and learned recommendations."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ranking
from .auth import require_auth
from .config import settings
from .db import session
from .models import AnalyticsCollection, Clip, MetricEvent, Publication

router = APIRouter(prefix="/api/v1/analytics")


def observations(db) -> list[ranking.Observation]:
    """Publications with a verified 7-day views observation (latest value per publication)."""
    metrics: dict[int, dict[str, float]] = {}
    for event in db.scalars(select(MetricEvent).where(MetricEvent.provenance == "verified", MetricEvent.window == "7d")
                            .order_by(MetricEvent.id)):
        metrics.setdefault(event.publication_id, {})[event.metric] = event.value
    rows = []
    for publication, clip in db.execute(select(Publication, Clip).join(Clip, Clip.id == Publication.clip_id)
                                        .where(Publication.id.in_(list(metrics)), Publication.published_at.is_not(None))):
        values = metrics[publication.id]
        if "views" not in values:
            continue
        rows.append(ranking.Observation(clip.score, clip.end_seconds - clip.start_seconds, publication.published_at.hour,
                                        values["views"], values.get("average_view_percentage")))
    return rows


@router.get("/recommendations")
def recommendations(db: Session = Depends(session), _: str = Depends(require_auth)) -> dict:
    return ranking.recommendations(observations(db), settings.learner_min_samples)


@router.get("/collections")
def collections(db: Session = Depends(session), _: str = Depends(require_auth)) -> list[dict]:
    query = select(AnalyticsCollection).order_by(AnalyticsCollection.id.desc()).limit(200)
    return [{key: getattr(row, key) for key in ("id", "publication_id", "window", "provider", "status", "reason", "attempted_at")}
            for row in db.scalars(query)]
