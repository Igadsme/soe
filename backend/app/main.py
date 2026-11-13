from contextlib import asynccontextmanager
import hmac
from pathlib import Path
from uuid import uuid4
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .db import Base, engine, session
from .auth import make_session, require_auth
from .config import settings
from .integrations import FixtureProvider, ProviderUnavailable, TwitchHelixProvider, YouTubeDataProvider
from .logging_config import configure_logging
from .media import ingest_authorized, render_vertical
from .models import Clip, LedgerEntry, MetricEvent, Publication, RightsRecord, Source, WorkflowRun
from .pipeline import extract_candidates
from .rights import can_ingest_and_publish, can_publish_to
from .schemas import ClipRead, RightsCreate, SourceCreate, SourceRead
from .transcription import FasterWhisperTranscriber, TranscriptionUnavailable

@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Soe API", version="0.2.0", lifespan=lifespan)



@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "soe-api"}


@app.post("/api/v1/auth/login")
async def login(username: str = Form(...), password: str = Form(...)) -> JSONResponse:
    if not settings.admin_password:
        if settings.demo_mode and settings.env == "development":
            return JSONResponse({"authenticated": True, "mode": "development-bypass"})
        raise HTTPException(503, "authentication is not configured")
    if not hmac.compare_digest(username, settings.admin_username) or not hmac.compare_digest(password, settings.admin_password):
        raise HTTPException(401, "invalid credentials")
    response = JSONResponse({"authenticated": True, "username": username})
    response.set_cookie("soe_session", make_session(username), httponly=True, secure=settings.env == "production", samesite="lax", max_age=settings.session_ttl_seconds)
    return response


@app.post("/api/v1/auth/logout")
async def logout() -> JSONResponse:
    response = JSONResponse({"authenticated": False})
    response.delete_cookie("soe_session")
    return response


@app.get("/api/v1/overview")
async def overview(db: Session = Depends(session)) -> dict:
    counts = {}
    for label, model in (("sources", Source), ("clips", Clip), ("workflows", WorkflowRun)):
        counts[label] = db.scalar(select(func.count()).select_from(model)) or 0
    revenue = db.scalar(select(func.coalesce(func.sum(LedgerEntry.cents), 0)).where(LedgerEntry.kind == "revenue", LedgerEntry.verified.is_(True))) or 0
    expenses = db.scalar(select(func.coalesce(func.sum(LedgerEntry.cents), 0)).where(LedgerEntry.kind == "expense", LedgerEntry.verified.is_(True))) or 0
    return {"environment": settings.env, "demo_mode": settings.demo_mode, "counts": counts, "revenue_cents": revenue, "expense_cents": expenses}


@app.post("/api/v1/sources/upload")
async def upload_source(file: UploadFile = File(...), title: str = Form(...), db: Session = Depends(session), _: str = Depends(require_auth)) -> dict:
    if file.content_type not in {"video/mp4", "video/quicktime", "video/webm", "application/octet-stream"}:
        raise HTTPException(415, "only MP4, QuickTime, or WebM uploads are accepted")
    media_root = Path(settings.media_root).resolve()
    media_root.mkdir(parents=True, exist_ok=True)
    destination = media_root / f"upload-{uuid4().hex}.mp4"
    size = 0
    with destination.open("wb") as output:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > settings.max_upload_bytes:
                destination.unlink(missing_ok=True)
                raise HTTPException(413, "upload exceeds configured size limit")
            output.write(chunk)
    source = Source(platform="upload", external_id=destination.stem, title=title, url="https://local.soe/upload", media_path=str(destination), status="rights_review")
    db.add(source)
    db.commit()
    return {"source_id": source.id, "status": source.status, "bytes": size, "filename": destination.name}


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


@app.post("/api/v1/sources/{source_id}/process")
async def process_source(source_id: int, db: Session = Depends(session), _: str = Depends(require_auth)) -> dict:
    source = db.get(Source, source_id)
    rights = db.scalar(select(RightsRecord).where(RightsRecord.source_id == source_id).order_by(RightsRecord.created_at.desc()))
    if not source or not rights:
        raise HTTPException(404, "source or rights record not found")
    if not can_ingest_and_publish(rights):
        raise HTTPException(403, "rights do not authorize processing")
    if not source.media_path or not Path(source.media_path).is_file():
        raise HTTPException(422, "source has no local authorized media path")
    run = WorkflowRun(source_id=source.id, state="transcribing")
    db.add(run)
    db.commit()
    try:
        ingested, digest, metadata = ingest_authorized(source.media_path, Path(settings.media_root) / "ingested" / f"{source.id}-{Path(source.media_path).name}")
        source.media_sha256 = digest
        segments = FasterWhisperTranscriber(settings.whisper_model_size).transcribe(ingested)
        candidates = extract_candidates(segments, limit=5)
        for index, candidate in enumerate(candidates, 1):
            clip_dir = Path(settings.media_root) / "clips"
            subtitle = clip_dir / f"source-{source.id}-clip-{index}.srt"
            output = clip_dir / f"source-{source.id}-clip-{index}.mp4"
            subtitle.parent.mkdir(parents=True, exist_ok=True)
            duration = max(1, int(candidate.end - candidate.start))
            subtitle.write_text(f"1\n00:00:00,000 --> 00:00:{duration:02},000\n{candidate.description}\n", encoding="utf-8")
            render_vertical(ingested, output, candidate.start, candidate.end, subtitle)
            db.add(Clip(source_id=source.id, start_seconds=candidate.start, end_seconds=candidate.end, description=candidate.description, hook=candidate.hook, score=candidate.score, status="rendered", evidence=candidate.evidence, output_path=str(output), subtitle_path=str(subtitle)))
        run.state = "edited"
        source.status = "analyzed"
        db.commit()
        return {"workflow_id": run.id, "source_id": source.id, "state": run.state, "probe": metadata, "transcript_segments": len(segments), "clips_generated": len(candidates)}
    except TranscriptionUnavailable as exc:
        run.state = "failed"
        run.error = str(exc)
        db.commit()
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        run.state = "failed"
        run.error = str(exc)
        db.commit()
        raise HTTPException(500, "processing failed; inspect workflow error") from exc


@app.get("/api/v1/clips", response_model=list[ClipRead])
async def list_clips(db: Session = Depends(session)) -> list[Clip]:
    return list(db.scalars(select(Clip).order_by(Clip.score.desc())).all())


@app.get("/api/v1/clips/{clip_id}/download")
async def download_clip(clip_id: int, db: Session = Depends(session), _: str = Depends(require_auth)) -> FileResponse:
    clip = db.get(Clip, clip_id)
    if not clip or not clip.output_path or not Path(clip.output_path).is_file():
        raise HTTPException(404, "rendered clip not found")
    return FileResponse(clip.output_path, media_type="video/mp4", filename=Path(clip.output_path).name)


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
