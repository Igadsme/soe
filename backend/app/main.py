from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from .db import Base, engine, session
from .models import Clip, RightsRecord, Source, WorkflowRun
from .rights import can_ingest_and_publish
from .schemas import ClipRead, RightsCreate, SourceCreate, SourceRead

app = FastAPI(title="Soe API", version="0.1.0")


@app.on_event("startup")
async def startup() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "soe-api"}


@app.get("/api/v1/overview")
async def overview(db: AsyncSession = Depends(session)) -> dict:
    counts = {}
    for label, model in (("sources", Source), ("clips", Clip), ("workflows", WorkflowRun)):
        counts[label] = await db.scalar(select(func.count()).select_from(model)) or 0
    return {"environment": "development", "demo_mode": True, "counts": counts, "revenue_cents": 0, "expense_cents": 0}


@app.post("/api/v1/sources", response_model=SourceRead, status_code=201)
async def create_source(payload: SourceCreate, db: AsyncSession = Depends(session)) -> Source:
    source = Source(platform=payload.platform, external_id=payload.external_id, title=payload.title, url=str(payload.url))
    db.add(source)
    await db.commit()
    await db.refresh(source)
    return source


@app.get("/api/v1/sources", response_model=list[SourceRead])
async def list_sources(db: AsyncSession = Depends(session)) -> list[Source]:
    return list((await db.scalars(select(Source).order_by(Source.created_at.desc()))).all())


@app.post("/api/v1/sources/{source_id}/rights", status_code=201)
async def create_rights(source_id: int, payload: RightsCreate, db: AsyncSession = Depends(session)) -> dict:
    source = await db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "source not found")
    record = RightsRecord(source_id=source_id, **payload.model_dump())
    db.add(record)
    source.status = "authorized" if can_ingest_and_publish(record) else "rights_review"
    await db.commit()
    return {"source_id": source_id, "status": source.status, "can_publish": can_ingest_and_publish(record)}


@app.get("/api/v1/clips", response_model=list[ClipRead])
async def list_clips(db: AsyncSession = Depends(session)) -> list[Clip]:
    return list((await db.scalars(select(Clip).order_by(Clip.score.desc()))).all())


@app.post("/api/v1/demo/pipeline")
async def demo_pipeline(db: AsyncSession = Depends(session)) -> dict:
    """Creates auditable local records only; no download, inference, or publishing is faked."""
    source = Source(platform="youtube", external_id="demo-authorized-source", title="Authorized demo source", url="https://example.com/authorized-source", status="authorized")
    db.add(source)
    await db.flush()
    rights = RightsRecord(source_id=source.id, authorization_type="owned", commercial_use=True, publishing_allowed=True, evidence="Local development fixture: owned media")
    db.add(rights)
    run = WorkflowRun(source_id=source.id, state="rights_verified", cost_cents=0)
    db.add(run)
    await db.commit()
    return {"workflow_id": run.id, "state": run.state, "note": "Fixture records only; connect authorized media and inference providers for real processing."}
