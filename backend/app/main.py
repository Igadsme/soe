from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .db import Base, engine, session
from .config import settings
from .integrations import FixtureProvider, ProviderUnavailable, TwitchHelixProvider, YouTubeDataProvider
from .logging_config import configure_logging
from .models import Clip, LedgerEntry, MetricEvent, Publication, RightsRecord, Source, WorkflowRun
from .rights import can_ingest_and_publish, can_publish_to
from .schemas import ClipRead, RightsCreate, SourceCreate, SourceRead

@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Soe API", version="0.2.0", lifespan=lifespan)



@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "soe-api"}


@app.get("/api/v1/overview")
async def overview(db: Session = Depends(session)) -> dict:
    counts = {}
    for label, model in (("sources", Source), ("clips", Clip), ("workflows", WorkflowRun)):
        counts[label] = db.scalar(select(func.count()).select_from(model)) or 0
    revenue = db.scalar(select(func.coalesce(func.sum(LedgerEntry.cents), 0)).where(LedgerEntry.kind == "revenue", LedgerEntry.verified.is_(True))) or 0
    expenses = db.scalar(select(func.coalesce(func.sum(LedgerEntry.cents), 0)).where(LedgerEntry.kind == "expense", LedgerEntry.verified.is_(True))) or 0
    return {"environment": settings.env, "demo_mode": settings.demo_mode, "counts": counts, "revenue_cents": revenue, "expense_cents": expenses}


@app.post("/api/v1/sources", response_model=SourceRead, status_code=201)
async def create_source(payload: SourceCreate, db: Session = Depends(session)) -> Source:
    source = Source(platform=payload.platform, external_id=payload.external_id, title=payload.title, url=str(payload.url), media_path=payload.media_path)
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


@app.get("/api/v1/sources", response_model=list[SourceRead])
async def list_sources(db: Session = Depends(session)) -> list[Source]:
    return list(db.scalars(select(Source).order_by(Source.created_at.desc())).all())


@app.post("/api/v1/sources/{source_id}/rights", status_code=201)
async def create_rights(source_id: int, payload: RightsCreate, db: Session = Depends(session)) -> dict:
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "source not found")
    record = RightsRecord(source_id=source_id, **payload.model_dump())
    db.add(record)
    source.status = "authorized" if can_ingest_and_publish(record) else "rights_review"
    db.commit()
    return {"source_id": source_id, "status": source.status, "can_publish": can_ingest_and_publish(record)}


@app.get("/api/v1/clips", response_model=list[ClipRead])
async def list_clips(db: Session = Depends(session)) -> list[Clip]:
    return list(db.scalars(select(Clip).order_by(Clip.score.desc())).all())


@app.post("/api/v1/discovery/search")
async def discover(platform: str, query: str, db: Session = Depends(session)) -> dict:
    if platform not in {"youtube", "twitch"}:
        raise HTTPException(422, "platform must be youtube or twitch")
    provider = (FixtureProvider(platform) if settings.demo_mode else YouTubeDataProvider() if platform == "youtube" else TwitchHelixProvider())
    try:
        items = await provider.search(query)
    except ProviderUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    created = 0
    for item in items:
        existing = db.scalar(select(Source).where(Source.external_id == item.external_id))
        if not existing:
            db.add(Source(platform=item.platform, external_id=item.external_id, title=item.title, url=item.url, metadata_json=item.metadata))
            created += 1
    db.commit()
    return {"provider": provider.__class__.__name__, "discovered": len(items), "created": created, "fixture": isinstance(provider, FixtureProvider)}


@app.get("/api/v1/publications")
async def publications(db: Session = Depends(session)) -> list[dict]:
    rows = list(db.scalars(select(Publication).order_by(Publication.created_at.desc())).all())
    return [{"id": row.id, "clip_id": row.clip_id, "platform": row.platform, "status": row.status, "external_id": row.external_id, "error": row.error} for row in rows]


@app.post("/api/v1/clips/{clip_id}/publish")
async def queue_publication(clip_id: int, platform: str, db: Session = Depends(session)) -> dict:
    if not settings.publishing_enabled:
        raise HTTPException(409, "publishing is disabled by SOE_PUBLISHING_ENABLED")
    clip = db.get(Clip, clip_id)
    if not clip:
        raise HTTPException(404, "clip not found")
    rights = db.scalar(select(RightsRecord).where(RightsRecord.source_id == clip.source_id).order_by(RightsRecord.created_at.desc()))
    if not rights or not can_publish_to(rights, platform):
        raise HTTPException(403, "current rights do not authorize publication to this platform")
    publication = Publication(clip_id=clip_id, platform=platform, status="queued")
    db.add(publication)
    db.commit()
    return {"publication_id": publication.id, "status": publication.status}


@app.get("/api/v1/analytics")
async def analytics(db: Session = Depends(session)) -> dict:
    events = list(db.scalars(select(MetricEvent)).all())
    return {"verified": [{"metric": event.metric, "value": event.value, "publication_id": event.publication_id} for event in events if event.verified], "unverified": [{"metric": event.metric, "value": event.value, "publication_id": event.publication_id} for event in events if not event.verified]}


@app.post("/api/v1/demo/pipeline")
async def demo_pipeline(db: Session = Depends(session)) -> dict:
    """Creates auditable local records only; no download, inference, or publishing is faked."""
    source = Source(platform="youtube", external_id="demo-authorized-source", title="Authorized demo source", url="https://example.com/authorized-source", status="authorized")
    db.add(source)
    db.flush()
    rights = RightsRecord(source_id=source.id, authorization_type="owned", commercial_use=True, publishing_allowed=True, evidence="Local development fixture: owned media")
    db.add(rights)
    run = WorkflowRun(source_id=source.id, state="rights_verified", cost_cents=0)
    db.add(run)
    db.commit()
    return {"workflow_id": run.id, "state": run.state, "note": "Fixture records only; connect authorized media and inference providers for real processing."}
