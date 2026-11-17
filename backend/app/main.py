import asyncio
import hmac
import logging
import re
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from hashlib import sha256
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from redis.exceptions import RedisError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .auth import (
    csrf_token,
    development_bypass,
    make_session,
    require_auth,
    revoke_session,
    verify_password,
)
from .config import secret_value, settings
from .db import session
from .integrations import (
    FixtureProvider,
    ProviderUnavailable,
    TwitchHelixProvider,
    YouTubeDataProvider,
)
from .logging_config import configure_logging
from .models import (
    Clip,
    Job,
    LedgerEntry,
    MetricEvent,
    Publication,
    RightsRecord,
    Source,
    WorkflowRun,
)
from .rate_limit import async_redis_limited, close_redis_clients, redis_ready
from .rights import can_ingest_and_publish, can_publish_to
from .schemas import ClipRead, RightsCreate, SourceCreate, SourceRead, safe_media_path


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    from .observability import init_sentry
    init_sentry()
    settings.validate_production()
    if settings.env in {"production", "staging"} and not settings.trusted_proxies.strip():
        logging.getLogger("soe.security").warning("SOE_TRUSTED_PROXIES is empty; configure trusted ingress peers when deployed behind a proxy")
    try:
        yield
    finally:
        await close_redis_clients()


app = FastAPI(title="Soe API", version="0.2.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=[item.strip() for item in settings.cors_origins.split(",") if item.strip()], allow_credentials=bool(settings.cors_origins.strip()), allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID", "Idempotency-Key"], expose_headers=["X-Request-ID"])

_rate_windows: dict[str, deque[float]] = defaultdict(deque)
_login_windows: dict[str, deque[float]] = defaultdict(deque)
_login_backoffs: dict[tuple[str, str], tuple[int, float]] = {}
_LOGIN_BACKOFF_MAX = 10000


def _prune_login_backoffs(protect: tuple[str, str]) -> None:
    """Bound backoff memory without letting username flooding reset the admin backoff."""
    if len(_login_backoffs) < _LOGIN_BACKOFF_MAX:
        return
    now = time.monotonic()
    for key in [k for k, (_, until) in _login_backoffs.items() if until <= now and k != protect]:
        del _login_backoffs[key]
    while len(_login_backoffs) >= _LOGIN_BACKOFF_MAX:
        victim = min((k for k in _login_backoffs if k != protect), key=lambda k: _login_backoffs[k][1], default=None)
        if victim is None:
            break
        del _login_backoffs[victim]


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    try:
        networks = [ip_network(n.strip()) for n in settings.trusted_proxies.split(",") if n.strip()]
        hops = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()] + [peer]
        for hop in reversed(hops):
            address = ip_address(hop)
            if not any(address in network for network in networks):
                return str(address)
    except ValueError:
        pass
    return peer


def limited(windows, key: str, limit: int) -> bool:
    now = time.monotonic()
    for old in list(windows):
        if not windows[old] or now - windows[old][-1] >= 60:
            del windows[old]
    if key not in windows and len(windows) >= 10000:
        # Fail closed under key flooding, without unbounded memory growth.
        return True
    window = windows[key]
    while window and now - window[0] >= 60:
        window.popleft()
    if len(window) >= limit:
        return True
    window.append(now)
    return False


async def request_limited(windows, key: str, limit: int) -> bool:
    if settings.redis_url.strip():
        scope = "login" if windows is _login_windows else "general"
        try:
            return await async_redis_limited(settings.redis_url, scope, key, limit)
        except (RedisError, ValueError):
            logging.getLogger("soe.rate_limit").warning("Redis rate limiter unavailable", extra={"scope": scope})
            if scope == "login":
                raise HTTPException(503, "login temporarily unavailable", headers={"Retry-After": "60"}) from None
            return False
    # Keep local window mutations serialized on the event loop.
    return limited(windows, key, limit)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, _exc: Exception):
    if secret_value(settings.sentry_dsn):
        import sentry_sdk
        sentry_sdk.capture_exception(_exc)
    request_id = getattr(request.state, "request_id", uuid4().hex)
    # Provider exception text and tracebacks may include credentials.
    logging.getLogger("soe.errors").error("unhandled request error", extra={"request_id": request_id})
    return JSONResponse({"detail": "internal server error", "request_id": request_id}, status_code=500)


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    # Do not echo submitted passwords or other sensitive inputs in validation errors.
    return JSONResponse({"detail": [{"loc": error["loc"], "type": error["type"], "msg": "invalid input"} for error in exc.errors()]}, status_code=422)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    started = time.monotonic()
    supplied = request.headers.get("X-Request-ID", "")
    request_id = supplied if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", supplied) else uuid4().hex
    request.state.request_id = request_id
    try:
        if await request_limited(_rate_windows, client_ip(request), settings.rate_limit_per_minute):
            response = JSONResponse({"detail": "rate limit exceeded"}, status_code=429, headers={"Retry-After": "60"})
        else:
            response = await call_next(request)
    except Exception as exc:
        response = await unhandled_error(request, exc)
    response.headers.update({
        "X-Request-ID": request_id,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "same-origin",
        "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Cross-Origin-Opener-Policy": "same-origin",
    })
    if settings.env in {"production", "staging"}:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    logging.getLogger("soe.access").info("request", extra={"request_id": request_id, "method": request.method, "path": request.url.path, "status": response.status_code, "duration_ms": round((time.monotonic() - started) * 1000, 2)})
    return response


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "soe-api"}


def database_ready(db: Session) -> dict:
    try:
        db.execute(text("SELECT 1"))
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        package_parent = Path(__file__).resolve().parents[1]
        root = package_parent if (package_parent / "backend" / "alembic").is_dir() else package_parent.parent
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "backend" / "alembic"))
        heads = set(ScriptDirectory.from_config(config).get_heads())
        current = set(db.execute(text("SELECT version_num FROM alembic_version")).scalars())
        if current != heads:
            raise HTTPException(503, "database migrations are pending")
    except SQLAlchemyError as exc:
        raise HTTPException(503, "database unavailable") from exc
    return {"status": "ready", "service": "soe-api"}


@app.get("/ready")
async def ready(db: Session = Depends(session)) -> dict:
    result = await run_in_threadpool(database_ready, db)
    if settings.redis_url and not await redis_ready(settings.redis_url):
        raise HTTPException(503, "redis unavailable")
    return result


@app.post("/api/v1/auth/login")
async def login(request: Request, username: str = Form(..., max_length=255), password: str = Form(..., max_length=1024)) -> JSONResponse:
    ip = client_ip(request)
    key = (username.casefold(), ip)
    failures, blocked_until = _login_backoffs.get(key, (0, 0))
    if time.monotonic() < blocked_until:
        raise HTTPException(429, "login temporarily unavailable", headers={"Retry-After": str(max(1, int(blocked_until - time.monotonic())))})
    if await request_limited(_login_windows, "pair:" + repr(key), 5) or await request_limited(_login_windows, "ip:" + ip, 20) or await request_limited(_login_windows, "user:" + username.casefold(), 30):
        raise HTTPException(429, "login temporarily unavailable", headers={"Retry-After": "60"})
    if not settings.admin_password_hash and not settings.admin_password:
        if development_bypass(request):
            return JSONResponse({"authenticated": True, "mode": "development-bypass"})
        raise HTTPException(503, "authentication is not configured")
    password_ok = await run_in_threadpool(verify_password, password, secret_value(settings.admin_password_hash)) if settings.admin_password_hash else settings.env == "development" and hmac.compare_digest(password.encode(), secret_value(settings.admin_password).encode())
    if not hmac.compare_digest(username.encode(), settings.admin_username.encode()) or not password_ok:
        _prune_login_backoffs(protect=(settings.admin_username.casefold(), ip))
        failures += 1
        _login_backoffs[key] = (failures, time.monotonic() + (min(3600, 2 ** min(failures - 4, 12)) if failures >= 5 else 0))
        raise HTTPException(401, "invalid credentials")
    _login_backoffs.pop(key, None)
    response = JSONResponse({"authenticated": True, "username": username})
    token = await run_in_threadpool(make_session, username)
    response.set_cookie("soe_session", token, httponly=True, secure=settings.env in {"production", "staging"}, samesite="lax", max_age=settings.session_ttl_seconds)
    response.set_cookie("soe_csrf", csrf_token(token), httponly=False, secure=settings.env in {"production", "staging"}, samesite="lax", path="/", max_age=settings.session_ttl_seconds)
    return response


@app.get("/api/v1/auth/me")
def current_user(username: str = Depends(require_auth)) -> dict:
    return {"username": username}


@app.post("/api/v1/auth/logout")
def logout(request: Request, _: str = Depends(require_auth)) -> JSONResponse:
    response = JSONResponse({"authenticated": False})
    if not development_bypass(request):
        revoke_session(request.cookies["soe_session"])
    for name in ("soe_session", "soe_csrf"):
        response.delete_cookie(name, path="/", secure=settings.env in {"production", "staging"}, httponly=name == "soe_session", samesite="lax")
    return response


@app.get("/api/v1/overview")
def overview(db: Session = Depends(session), _: str = Depends(require_auth)) -> dict:
    counts = {}
    for label, model in (("sources", Source), ("clips", Clip), ("workflows", WorkflowRun)):
        counts[label] = db.scalar(select(func.count()).select_from(model)) or 0
    revenue = db.scalar(select(func.coalesce(func.sum(LedgerEntry.cents), 0)).where(LedgerEntry.kind == "revenue", LedgerEntry.verified.is_(True))) or 0
    expenses = db.scalar(select(func.coalesce(func.sum(LedgerEntry.cents), 0)).where(LedgerEntry.kind == "expense", LedgerEntry.verified.is_(True))) or 0
    return {"environment": settings.env, "demo_mode": settings.demo_mode, "counts": counts, "revenue_cents": revenue, "expense_cents": expenses}


@app.post("/api/v1/sources/upload")
def upload_source(file: UploadFile = File(...), title: str = Form(..., min_length=1, max_length=500), db: Session = Depends(session), _: str = Depends(require_auth)) -> dict:
    extensions = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"}
    if file.content_type not in extensions:
        raise HTTPException(415, "unsupported media type")
    media_root = Path(settings.media_root).resolve()
    media_root.mkdir(parents=True, exist_ok=True)
    destination = media_root / f"upload-{uuid4().hex}{extensions[file.content_type]}"
    success = False
    try:
        prefix = file.file.read(4096)
        brands = {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"qt  ", b"M4V ", b"dash"}
        valid = (len(prefix) >= 12 and prefix[4:8] == b"ftyp" and prefix[8:12] in brands) if file.content_type != "video/webm" else prefix.startswith(b"\x1a\x45\xdf\xa3") and b"\x42\x82\x84webm" in prefix
        if not valid:
            raise HTTPException(415, "invalid media signature")
        file.file.seek(0)
        size = 0
        with destination.open("wb") as output:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(413, "upload exceeds configured size limit")
                output.write(chunk)
        source = Source(platform="upload", external_id=destination.stem, title=title, url="https://local.soe/upload", media_path=str(destination), status="rights_review")
        db.add(source)
        db.commit()
        success = True
        return {"source_id": source.id, "status": source.status, "bytes": size, "filename": destination.name}
    finally:
        if not success:
            destination.unlink(missing_ok=True)


@app.post("/api/v1/sources", response_model=SourceRead, status_code=201)
def create_source(payload: SourceCreate, db: Session = Depends(session), _: str = Depends(require_auth)) -> Source:
    source = Source(platform=payload.platform, external_id=payload.external_id, title=payload.title, url=str(payload.url), media_path=payload.media_path)
    db.add(source)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "source external_id already exists") from None
    db.refresh(source)
    return source


@app.get("/api/v1/sources", response_model=list[SourceRead])
def list_sources(db: Session = Depends(session), _: str = Depends(require_auth)) -> list[Source]:
    return list(db.scalars(select(Source).order_by(Source.created_at.desc())).all())


@app.post("/api/v1/sources/{source_id}/rights", status_code=201)
def create_rights(payload: RightsCreate, source_id: int = PathParam(gt=0, le=2147483647), db: Session = Depends(session), _: str = Depends(require_auth)) -> dict:
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "source not found")
    if payload.authorization_type == "owned" and source.platform != "upload":
        raise HTTPException(422, "owned authorization requires an upload source")
    record = RightsRecord(source_id=source_id, **payload.model_dump())
    db.add(record)
    source.status = "authorized" if can_ingest_and_publish(record) else "rights_review"
    db.commit()
    return {"source_id": source_id, "status": source.status, "can_publish": can_ingest_and_publish(record)}


@app.post("/api/v1/sources/{source_id}/process", status_code=202)
def process_source(request: Request, source_id: int = PathParam(gt=0, le=2147483647), db: Session = Depends(session), _: str = Depends(require_auth)) -> dict:
    source = db.get(Source, source_id)
    rights = db.scalar(select(RightsRecord).where(RightsRecord.source_id == source_id).order_by(RightsRecord.created_at.desc()))
    if not source or not rights:
        raise HTTPException(404, "source or rights record not found")
    if not can_ingest_and_publish(rights):
        raise HTTPException(403, "rights do not authorize processing")
    try:
        if source.media_path:
            safe_media_path(source.media_path)
    except ValueError as exc:
        raise HTTPException(422, "invalid media path") from exc
    if not source.media_path or not Path(source.media_path).is_file():
        raise HTTPException(422, "source has no local authorized media path")
    raw_key = request.headers.get("Idempotency-Key")
    if raw_key is not None and not 1 <= len(raw_key) <= 255:
        raise HTTPException(422, "invalid idempotency key")
    key = sha256(raw_key.encode()).hexdigest() if raw_key else None
    existing = db.scalar(select(Job).where(Job.idempotency_key == key)) if key else None
    if existing and existing.source_id != source_id:
        raise HTTPException(409, "idempotency key already used")
    existing = existing or db.scalar(select(Job).where(Job.source_id == source_id, Job.status.in_(["queued", "running"])))
    if existing:
        return JSONResponse({"job_id": existing.id, "status": existing.status}, status_code=200)
    run = WorkflowRun(source_id=source_id, state="queued")
    try:
        db.add(run)
        db.flush()
        job = Job(id=uuid4().hex, source_id=source_id, workflow_id=run.id, idempotency_key=key)
        db.add(job)
        db.commit()
    except IntegrityError:
        db.rollback()
        job = db.scalar(select(Job).where(Job.source_id == source_id, Job.status.in_(["queued", "running"])))
        if not job and key:
            job = db.scalar(select(Job).where(Job.idempotency_key == key))
        if not job or job.source_id != source_id:
            raise HTTPException(409, "idempotency conflict") from None
    return {"job_id": job.id, "status": job.status}


def job_read(job: Job) -> dict:
    return {name: getattr(job, name) for name in ("id", "kind", "source_id", "status", "attempts", "max_attempts", "run_after", "last_error", "result", "created_at", "updated_at")}


@app.get("/api/v1/jobs")
def list_jobs(db: Session = Depends(session), _: str = Depends(require_auth)) -> list[dict]:
    return [job_read(job) for job in db.scalars(select(Job).order_by(Job.created_at.desc()).limit(100))]


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(session), _: str = Depends(require_auth)) -> dict:
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job_read(job)


@app.get("/api/v1/clips", response_model=list[ClipRead])
def list_clips(db: Session = Depends(session), _: str = Depends(require_auth)) -> list[Clip]:
    return list(db.scalars(select(Clip).order_by(Clip.score.desc())).all())


@app.get("/api/v1/clips/{clip_id}/download")
def download_clip(clip_id: int = PathParam(gt=0, le=2147483647), db: Session = Depends(session), _: str = Depends(require_auth)) -> FileResponse:
    clip = db.get(Clip, clip_id)
    if not clip or not clip.output_path or not Path(clip.output_path).is_file():
        raise HTTPException(404, "rendered clip not found")
    return FileResponse(clip.output_path, media_type="video/mp4", filename=Path(clip.output_path).name)


@app.post("/api/v1/discovery/search")
def discover(platform: Literal["youtube", "twitch"], query: str = Query(min_length=1, max_length=200), db: Session = Depends(session), _: str = Depends(require_auth)) -> dict:
    if platform not in {"youtube", "twitch"}:
        raise HTTPException(422, "platform must be youtube or twitch")
    provider = (FixtureProvider(platform) if settings.demo_mode else YouTubeDataProvider() if platform == "youtube" else TwitchHelixProvider())
    try:
        items = asyncio.run(provider.search(query))
    except ProviderUnavailable as exc:
        raise HTTPException(exc.status_code, "service temporarily unavailable") from exc
    created = 0
    for item in items:
        existing = db.scalar(select(Source).where(Source.external_id == item.external_id))
        if not existing:
            db.add(Source(platform=item.platform, external_id=item.external_id, title=item.title, url=item.url, metadata_json=item.metadata))
            created += 1
    db.commit()
    return {"provider": provider.__class__.__name__, "discovered": len(items), "created": created, "fixture": isinstance(provider, FixtureProvider)}


@app.get("/api/v1/publications")
def publications(db: Session = Depends(session), _: str = Depends(require_auth)) -> list[dict]:
    rows = list(db.scalars(select(Publication).order_by(Publication.created_at.desc())).all())
    return [{"id": row.id, "clip_id": row.clip_id, "platform": row.platform, "status": row.status, "external_id": row.external_id, "error": "publication failed" if row.error else None} for row in rows]


@app.post("/api/v1/clips/{clip_id}/publish")
def queue_publication(platform: Literal["youtube", "twitch", "tiktok", "instagram"], clip_id: int = PathParam(gt=0, le=2147483647), db: Session = Depends(session), _: str = Depends(require_auth)) -> dict:
    if not settings.publishing_enabled:
        raise HTTPException(409, "publishing is disabled by SOE_PUBLISHING_ENABLED")
    clip = db.get(Clip, clip_id)
    if not clip:
        raise HTTPException(404, "clip not found")
    rights = db.scalar(select(RightsRecord).where(RightsRecord.source_id == clip.source_id).order_by(RightsRecord.created_at.desc()))
    if not rights or not can_publish_to(rights, platform):
        raise HTTPException(403, "current rights do not authorize publication to this platform")
    publication = db.scalar(select(Publication).where(Publication.clip_id == clip_id, Publication.platform == platform))
    if publication:
        return {"publication_id": publication.id, "status": publication.status}
    publication = Publication(clip_id=clip_id, platform=platform, status="queued")
    db.add(publication)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        publication = db.scalar(select(Publication).where(Publication.clip_id == clip_id, Publication.platform == platform))
    return {"publication_id": publication.id, "status": publication.status}


@app.get("/api/v1/analytics")
def analytics(db: Session = Depends(session), _: str = Depends(require_auth)) -> dict:
    events = list(db.scalars(select(MetricEvent)).all())
    return {"verified": [{"metric": event.metric, "value": event.value, "publication_id": event.publication_id} for event in events if event.verified], "unverified": [{"metric": event.metric, "value": event.value, "publication_id": event.publication_id} for event in events if not event.verified]}


@app.get("/api/v1/revenue")
def revenue(db: Session = Depends(session), _: str = Depends(require_auth)) -> dict:
    entries = list(db.scalars(select(LedgerEntry).order_by(LedgerEntry.created_at.desc())).all())
    return {"entries": [{"id": item.id, "kind": item.kind, "category": item.category, "cents": item.cents, "verified": item.verified, "note": item.note} for item in entries], "verified_only": True}


@app.get("/api/v1/agents")
def agents(db: Session = Depends(session), _: str = Depends(require_auth)) -> list[dict]:
    queued = db.scalar(select(func.count()).select_from(WorkflowRun).where(WorkflowRun.state == "queued")) or 0
    live_discovery = bool(settings.youtube_api_key or (settings.twitch_client_id and settings.twitch_client_secret))
    return [{"name": name, "status": ("ready" if ready else "blocked"), "detail": detail, "queued_workflows": queued} for name, ready, detail in [
        ("discovery", live_discovery or settings.demo_mode, "official adapters configured" if live_discovery else "fixture mode (development)" if settings.demo_mode else "blocked: no provider credentials configured"),
        ("rights", True, "fail-closed policy active"),
        ("perception", True, f"Faster-Whisper {settings.whisper_model_size} boundary available"),
        ("highlights", True, "evidence-backed ranking active"),
        ("editing", True, "FFmpeg renderer available"),
        ("publishing", settings.publishing_enabled, "publishing feature flag"),
        ("analytics", True, "verified/unverified ledger active"),
        ("optimization", False, "requires observed performance dataset"),
    ]]


@app.get("/api/v1/settings")
def read_settings(_: str = Depends(require_auth)) -> dict:
    return {"environment": settings.env, "demo_mode": settings.demo_mode, "whisper_model_size": settings.whisper_model_size, "publishing_enabled": settings.publishing_enabled, "max_upload_bytes": settings.max_upload_bytes, "max_clip_seconds": settings.max_clip_seconds, "configured_integrations": {"youtube": bool(settings.youtube_api_key), "twitch": bool(settings.twitch_client_id and settings.twitch_client_secret)}}


@app.post("/api/v1/demo/pipeline")
def demo_pipeline(db: Session = Depends(session), _: str = Depends(require_auth)) -> dict:
    """Creates auditable local records only; no download, inference, or publishing is faked."""
    if settings.env != "development" or not settings.demo_mode:
        raise HTTPException(404, "not found")
    source = Source(platform="youtube", external_id="demo-authorized-source", title="Authorized demo source", url="https://example.com/authorized-source", status="authorized")
    db.add(source)
    db.flush()
    rights = RightsRecord(source_id=source.id, authorization_type="owned", commercial_use=True, publishing_allowed=True, evidence="Local development fixture: owned media")
    db.add(rights)
    run = WorkflowRun(source_id=source.id, state="rights_verified", cost_cents=0)
    db.add(run)
    db.commit()
    return {"workflow_id": run.id, "state": run.state, "note": "Fixture records only; connect authorized media and inference providers for real processing."}


@app.post("/api/v1/auth/logout-all")
def logout_all(request: Request, username: str = Depends(require_auth)) -> dict:
    from sqlalchemy import update

    from .auth import SessionLocal
    from .models import AuthSession
    with SessionLocal() as db:
        db.execute(update(AuthSession).where(AuthSession.username == username).values(revoked_at=int(time.time())))
        db.commit()
    return {"authenticated": False}


from .request_guard import RequestGuard  # noqa: E402

app.add_middleware(RequestGuard)


from .observability import MetricsMiddleware, metrics_response  # noqa: E402


@app.get("/metrics")
def metrics(request: Request, db: Session = Depends(session)):
    return metrics_response(request, db)


app.add_middleware(MetricsMiddleware)
