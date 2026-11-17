"""Regression tests for the independent Sentinel security and worker review."""
import asyncio
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, text, create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app import auth, main, media, rate_limit, worker
from app.config import Settings
from app.models import AuthSession, Clip, Job, WorkflowRun
from app.rights import can_ingest_and_publish, can_publish_to
from app.schemas import RightsCreate
from app.subtitles import srt_timestamp
from test_jobs import queued, enqueue, stub_media  # noqa: F401


@pytest.mark.asyncio
async def test_health_during_slow_worker(queued, monkeypatch):
    enqueue(queued)
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    def slow(*args):
        loop.call_soon_threadsafe(started.set)
        time.sleep(0.4)
    monkeypatch.setattr(worker, 'process_job', slow)
    task = asyncio.create_task(worker.process_once())
    await started.wait()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
        before = time.monotonic()
        assert (await client.get('/health')).status_code == 200
        assert time.monotonic() - before < 0.2
    await task


def test_upload_guard_never_reads_unauthenticated_body(monkeypatch):
    monkeypatch.setattr(auth.settings, 'dev_auth_bypass', False)
    async def exercise():
        async def receive():
            pytest.fail('body was read before authentication')
        messages = []
        async def send(message): messages.append(message)
        scope = {'type':'http', 'method':'POST', 'path':'/api/v1/sources/upload', 'headers':[(b'content-length', b'50000000')], 'client':('127.0.0.1', 1)}
        from app.request_guard import RequestGuard
        await RequestGuard(main.app)(scope, receive, send)
        assert messages[0]['status'] == 401
    asyncio.run(exercise())
    assert not Path(auth.settings.media_root).exists()


def test_oversized_content_length_and_streamed_body():
    with TestClient(main.app) as client:
        assert client.post('/api/v1/auth/login', content=b'', headers={'Content-Length':'70000'}).status_code == 413
        assert client.post('/api/v1/auth/login', content=iter([b'x'*40000, b'y'*40000])).status_code == 413


def test_proxy_chain_right_to_left_and_bypass(monkeypatch):
    monkeypatch.setattr(main.settings, 'trusted_proxies', '127.0.0.1/32,10.0.0.0/8')
    request = Request({'type':'http','method':'GET','client':('127.0.0.1',1),'headers':[(b'x-forwarded-for', b'198.51.100.1, 192.0.2.8, 10.0.0.1')]})
    assert main.client_ip(request) == '192.0.2.8'
    assert auth.development_bypass(request)
    request.scope['client'] = ('192.0.2.1',1)
    assert not auth.development_bypass(request)
    monkeypatch.setattr(auth.settings, 'dev_auth_bypass', False)
    request.scope['client'] = ('127.0.0.1',1)
    assert not auth.development_bypass(request)


def test_username_backoff_rotating_forwarded_ips(monkeypatch):
    monkeypatch.setattr(main.settings, 'trusted_proxies', '')
    monkeypatch.setattr(auth.settings, 'admin_password', 'test-only-password')
    with TestClient(main.app) as client:
        for index in range(5):
            assert client.post('/api/v1/auth/login', data={'username':'operator','password':'wrong'}, headers={'X-Forwarded-For':f'192.0.2.{index}'}).status_code == 401
        assert client.post('/api/v1/auth/login', data={'username':'operator','password':'wrong'}, headers={'X-Forwarded-For':'192.0.2.99'}).status_code == 429
        assert main._login_backoffs[('operator', '127.0.0.1')][0] == 5


def test_production_defaults_staging_and_publishing(monkeypatch):
    for name in ('SOE_ENV', 'SOE_DEMO_MODE'):
        monkeypatch.delenv(name)
    config = Settings(_env_file=None)
    assert config.env == 'production' and not config.demo_mode
    with pytest.raises(ValidationError): Settings(_env_file=None, env='typo')
    for env in ('staging', 'production'):
        with pytest.raises(RuntimeError, match='SOE_ADMIN_PASSWORD is forbidden'):
            Settings(_env_file=None, env=env, admin_password='test').validate_production()
        with pytest.raises(RuntimeError, match='SOE_PUBLISHING_ENABLED'):
            Settings(_env_file=None, env=env, publishing_enabled=True).validate_production()


@pytest.mark.asyncio
async def test_hung_redis_concurrent_health_and_circuit(monkeypatch):
    class Hung:
        async def eval(self, *args): await asyncio.sleep(30)
        async def ping(self): await asyncio.sleep(30)
        async def aclose(self): pass
    monkeypatch.setattr(rate_limit.AsyncRedis, 'from_url', lambda *a, **k: Hung())
    monkeypatch.setattr(main.settings, 'redis_url', 'redis://stub')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
        start = time.monotonic()
        responses = await asyncio.gather(*(client.get('/health') for _ in range(8)))
        assert all(r.status_code == 200 for r in responses)
        assert time.monotonic() - start < 0.65
        start = time.monotonic()
        assert (await client.get('/health')).status_code == 200
        assert time.monotonic() - start < 0.1
        assert not await rate_limit.redis_ready('redis://stub')


@pytest.mark.asyncio
async def test_real_redis_lua():
    url = os.environ.get('SOE_TEST_REDIS_URL')
    if not url: pytest.skip('SOE_TEST_REDIS_URL not set')
    import uuid
    key = uuid.uuid4().hex
    assert not await rate_limit.async_redis_limited(url, 'test', key, 1)
    assert await rate_limit.async_redis_limited(url, 'test', key, 1)


def test_rights_validation_scope_and_naive_expiry():
    payload = dict(authorization_type='owned', commercial_use=True, publishing_allowed=True, evidence='Documented permission for this media', allowed_platforms=['youtube'])
    for update in ({'evidence':' '*40}, {'allowed_platforms':[]}, {'allowed_platforms':['unknown']}):
        with pytest.raises(ValidationError): RightsCreate(**{**payload, **update})
    valid = RightsCreate(**payload, expires_at=datetime(2099,1,1))
    assert valid.expires_at.tzinfo == timezone.utc
    record = SimpleNamespace(**valid.model_dump(), revoked=False)
    record.expires_at = datetime(2099,1,1)
    assert can_ingest_and_publish(record)
    record.allowed_platforms = ['unknown']
    assert not can_ingest_and_publish(record)
    record.allowed_platforms = []
    assert not can_publish_to(record, 'youtube') and not can_ingest_and_publish(record)
    with TestClient(main.app) as client:
        source = client.post('/api/v1/sources', json={'platform':'youtube','external_id':'remote','title':'Title','url':'https://example.com'}).json()
        assert client.post(f"/api/v1/sources/{source['id']}/rights", json=payload).status_code == 422


def test_duplicate_source_returns_conflict():
    payload = {'platform':'upload','external_id':'duplicate','title':'Title','url':'https://example.com'}
    with TestClient(main.app) as client:
        assert client.post('/api/v1/sources', json=payload).status_code == 201
        assert client.post('/api/v1/sources', json=payload).status_code == 409


def test_failed_render_rolls_back_files_and_rerun_replaces(queued, isolated_database, monkeypatch):
    stub_media(monkeypatch)
    job_id = enqueue(queued)
    worker.claim_job('first')
    worker.process_job(job_id, 'first')
    second = enqueue(queued)
    worker.claim_job('second')
    def broken(*args):
        args[1].write_bytes(b'partial')
        raise RuntimeError('render failed')
    monkeypatch.setattr(worker, 'render_vertical', broken)
    with pytest.raises(RuntimeError): worker.process_job(second, 'second')
    with isolated_database() as db:
        assert len(list(db.scalars(select(Clip)))) == 1
        run_id = db.get(Job, second).workflow_id
    assert not (Path(main.settings.media_root)/'clips'/f'source-{queued}'/f'run-{run_id}'/'second').exists()
    stub_media(monkeypatch)
    worker.process_job(second, 'second')
    with isolated_database() as db:
        clips = list(db.scalars(select(Clip)))
        assert len(clips) == 1 and f'run-{run_id}' in clips[0].output_path


@pytest.mark.parametrize('seconds,expected', [(59.5,'00:00:59,500'),(61,'00:01:01,000'),(3661.25,'01:01:01,250')])
def test_srt_timestamps(seconds, expected):
    assert srt_timestamp(seconds) == expected


def test_media_timeout_and_duration_cap(tmp_path, monkeypatch):
    source = tmp_path/'input.mp4'
    source.write_bytes(b'video')
    monkeypatch.setattr(media, 'probe', lambda _: media.MediaProbe(3601,10,10,True))
    with pytest.raises(media.MediaProcessingError): media.ingest_authorized(str(source), tmp_path/'out.mp4')
    monkeypatch.setattr(media, '_binary', lambda _: '/stub/ffmpeg')
    def timeout(*args, **kwargs):
        assert kwargs['timeout'] == media.settings.ffmpeg_timeout_seconds
        raise subprocess.TimeoutExpired('ffmpeg', kwargs['timeout'])
    monkeypatch.setattr(media.subprocess, 'run', timeout)
    with pytest.raises(subprocess.TimeoutExpired): media.render_vertical(source, tmp_path/'out.mp4',0,1)


def test_session_binding_idle_password_change_logout_all(isolated_database, monkeypatch):
    monkeypatch.setattr(auth.settings,'session_secret','test-session-secret'*3)
    monkeypatch.setattr(auth.settings,'admin_password','test-password')
    with TestClient(main.app) as client:
        assert client.post('/api/v1/auth/login',data={'username':'operator','password':'test-password'}).status_code == 200
        token = client.cookies['soe_session']
        other = auth.make_session('operator')
        assert auth.csrf_token(token) != auth.csrf_token(other)
        assert client.post('/api/v1/auth/logout-all',headers={'X-CSRF-Token':auth.csrf_token(other)}).status_code == 403
        assert client.post('/api/v1/auth/logout-all',headers={'X-CSRF-Token':auth.csrf_token(token)}).status_code == 200
        for value in (token, other):
            with pytest.raises(HTTPException): auth.verify_session(value)
    token = auth.make_session('operator')
    with isolated_database() as db:
        row = db.scalar(select(AuthSession).where(AuthSession.revoked_at.is_(None)))
        row.last_seen_at = int(time.time()) - 61
        db.commit()
    assert auth.verify_session(token) == 'operator'
    with isolated_database() as db:
        row = db.scalar(select(AuthSession).where(AuthSession.revoked_at.is_(None)))
        row.last_seen_at = int(time.time()) - 1801
        db.commit()
    with pytest.raises(HTTPException): auth.verify_session(token)
    token = auth.make_session('operator')
    monkeypatch.setattr(auth.settings,'admin_password_hash','changed')
    with pytest.raises(HTTPException): auth.verify_session(token)
    hashed = auth.make_password_hash('test')
    assert hashed.startswith('pbkdf2_sha256$600000$') and auth.verify_password('test',hashed)


@pytest.mark.parametrize('mime,data,extension', [('video/webm',b'\x1a\x45\xdf\xa3\x42\x82\x84webm','.webm'),('video/quicktime',b'\x00\x00\x00\x18ftypqt  ','.mov')])
def test_upload_extensions_and_partial_cleanup(mime, data, extension, monkeypatch):
    with TestClient(main.app) as client:
        response = client.post('/api/v1/sources/upload',data={'title':'test'},files={'file':('input',data,mime)})
        assert response.status_code == 200 and response.json()['filename'].endswith(extension)
        assert client.post('/api/v1/sources/upload',data={'title':'test'},files={'file':('input',b'\x1a\x45\xdf\xa3junk','video/webm')}).status_code == 415


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [401,403,'timeout','json'])
@pytest.mark.parametrize('platform',['youtube','twitch'])
async def test_provider_failures_generic(monkeypatch, code, platform):
    from app import integrations
    monkeypatch.setattr(main.settings, 'demo_mode', False)
    monkeypatch.setattr(main.settings,'youtube_api_key','test')
    monkeypatch.setattr(main.settings,'twitch_client_id','test')
    monkeypatch.setattr(main.settings,'twitch_client_secret','test')
    real = httpx.AsyncClient
    def handler(request):
        if code == 'timeout': raise httpx.ReadTimeout('private',request=request)
        if code == 'json': return httpx.Response(200,content=b'bad json')
        return httpx.Response(code)
    monkeypatch.setattr(integrations.httpx, 'AsyncClient',lambda **kwargs: real(transport=httpx.MockTransport(handler),**kwargs))
    async with real(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
        response = await client.post(f'/api/v1/discovery/search?platform={platform}&query=test')
    assert response.status_code == (502 if code == 'json' else 503)
    assert response.json()['detail'] == 'service temporarily unavailable'


def test_real_postgres_claim_skip_locked(monkeypatch):
    url = os.environ.get('SOE_TEST_POSTGRES_URL')
    if not url: pytest.skip('SOE_TEST_POSTGRES_URL not set')
    from app.db import Base
    from app.models import Source
    engine = create_engine(url)
    schema = 'sentinel_claim_test'
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS {schema}'))
    test_engine = create_engine(url, connect_args={'options':f'-csearch_path={schema}'})
    Base.metadata.create_all(test_engine)
    factory = sessionmaker(test_engine)
    monkeypatch.setattr(worker,'SessionLocal',factory)
    try:
        with factory() as db:
            source = Source(platform='upload',external_id='claim',title='test',url='https://example.com')
            db.add(source); db.flush()
            run = WorkflowRun(source_id=source.id); db.add(run); db.flush()
            db.add(Job(id='real-postgres',source_id=source.id,workflow_id=run.id)); db.commit()
        with factory() as locked:
            locked.execute(select(Job).with_for_update())
            assert worker.claim_job('blocked') is None
        assert worker.claim_job('owner') == 'real-postgres'
    finally:
        test_engine.dispose()
        with engine.begin() as connection: connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        engine.dispose()


def test_expired_worker_lease_updates_workflow(queued, isolated_database):
    job_id = enqueue(queued)
    worker.claim_job('killed')
    with isolated_database() as db:
        job = db.get(Job, job_id)
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.get(WorkflowRun, job.workflow_id).state = 'transcribing'
        db.commit()
    assert worker.claim_job('replacement') == job_id
    with isolated_database() as db:
        job = db.get(Job, job_id)
        assert job.attempts == 2 and job.locked_by == 'replacement'
        assert db.get(WorkflowRun, job.workflow_id).state == 'queued'


def test_publication_idempotency_unique_constraint(isolated_database, monkeypatch):
    from app.models import Source, RightsRecord, Publication
    from sqlalchemy.exc import IntegrityError
    monkeypatch.setattr(main.settings,'publishing_enabled',True)
    with isolated_database() as db:
        source = Source(platform='upload',external_id='publish',title='test',url='https://example.com')
        db.add(source); db.flush()
        clip = Clip(source_id=source.id,start_seconds=0,end_seconds=1,description='test',hook='test',score=1)
        db.add(clip); db.flush()
        clip_id = clip.id
        db.add(RightsRecord(source_id=source.id,authorization_type='owned',commercial_use=True,publishing_allowed=True,allowed_platforms=['youtube'],evidence='Owned authorization document'))
        db.commit()
    with TestClient(main.app) as client:
        first = client.post(f'/api/v1/clips/{clip_id}/publish?platform=youtube')
        second = client.post(f'/api/v1/clips/{clip_id}/publish?platform=youtube')
        assert first.status_code == 200 and first.json() == second.json()
    with isolated_database() as db:
        assert len(list(db.scalars(select(Publication)))) == 1
        db.add(Publication(clip_id=clip_id,platform='youtube'))
        with pytest.raises(IntegrityError): db.commit()


def test_upload_db_failure_removes_partial_file(monkeypatch, isolated_database):
    from app.db import session
    def broken_session():
        with isolated_database() as db:
            def fail(): raise RuntimeError('database error')
            monkeypatch.setattr(db,'commit',fail)
            yield db
    main.app.dependency_overrides[session] = broken_session
    with TestClient(main.app) as client:
        response = client.post('/api/v1/sources/upload',data={'title':'test'},files={'file':('test.mp4',b'\x00\x00\x00\x18ftypisom','video/mp4')})
        assert response.status_code == 500
    assert not list(Path(main.settings.media_root).iterdir())


def test_idle_session_pruning(isolated_database, monkeypatch):
    monkeypatch.setattr(auth.settings,'session_retention_seconds',0)
    with isolated_database() as db:
        db.add(AuthSession(id='idle',username='operator',expires_at=int(time.time())+10000,last_seen_at=int(time.time())-1801))
        db.commit()
    assert worker.cleanup_sessions() == 1


@pytest.mark.asyncio
async def test_ready_requires_configured_redis(isolated_database, monkeypatch):
    with isolated_database() as db:
        db.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32))'))
        db.execute(text("INSERT INTO alembic_version VALUES ('0005_security')")); db.commit()
    async def unavailable(url): return False
    monkeypatch.setattr(main,'redis_ready',unavailable)
    monkeypatch.setattr(main.settings,'redis_url','redis://stub')
    async def allowed(*args): return False
    monkeypatch.setattr(main,'async_redis_limited',allowed)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
        assert (await client.get('/ready')).status_code == 503


@pytest.mark.asyncio
async def test_staging_secure_cookies_and_hsts(monkeypatch):
    monkeypatch.setattr(auth.settings,'env','staging')
    monkeypatch.setattr(auth.settings,'session_secret','staging-test-secret'*3)
    monkeypatch.setattr(auth.settings,'admin_password','')
    monkeypatch.setattr(auth.settings,'admin_password_hash',auth.make_password_hash('test'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='https://test') as client:
        response = await client.post('/api/v1/auth/login',data={'username':'operator','password':'test'})
        assert response.status_code == 200
        assert 'Strict-Transport-Security' in response.headers
        assert all('Secure' in value for value in response.headers.get_list('set-cookie'))


def test_legacy_password_hash_warns(caplog):
    salt = b'0123456789abcdef'
    stored = salt.hex() + '$' + auth.hash_password('test',salt)
    assert auth.verify_password('test',stored)
    assert 'regenerate SOE_ADMIN_PASSWORD_HASH' in caplog.text
    assert not auth.verify_password('test','malformed')


def test_migration_publication_dedup_preserves_metrics(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from app import db as app_db
    url = f'sqlite:///{tmp_path / "migration.db"}'
    monkeypatch.setattr(app_db,'url',url)
    config = Config(str(Path(__file__).resolve().parents[2]/'alembic.ini'))
    command.upgrade(config,'0004_jobs')
    engine = create_engine(url)
    with engine.begin() as db:
        for publication_id in (1,2):
            db.execute(text("INSERT INTO publications (id,clip_id,platform,status,created_at) VALUES (:id,1,'youtube','queued',CURRENT_TIMESTAMP)"), {'id':publication_id})
        db.execute(text("INSERT INTO metric_events (publication_id,metric,value,verified,observed_at) VALUES (2,'views',10,1,CURRENT_TIMESTAMP)"))
    command.upgrade(config,'head')
    with engine.connect() as db:
        assert db.scalar(text('SELECT COUNT(*) FROM publications')) == 1
        assert db.scalar(text('SELECT publication_id FROM metric_events')) == 1
    engine.dispose()


def test_login_backoff_survives_username_flooding(monkeypatch):
    import time as _time
    from app import main as _main
    from app.config import settings as _settings

    monkeypatch.setattr(_main, "_LOGIN_BACKOFF_MAX", 50)
    admin_key = (_settings.admin_username.casefold(), "127.0.0.1")
    _main._login_backoffs[admin_key] = (6, _time.monotonic() + 120)
    for i in range(200):
        _main._login_backoffs[(f"flood{i}", "127.0.0.1")] = (1, 0)
        _main._prune_login_backoffs(protect=admin_key)
    assert _main._login_backoffs[admin_key][0] == 6
    assert len(_main._login_backoffs) < 50


def test_login_backoff_isolated_by_trusted_client_ip(monkeypatch):
    monkeypatch.setattr(main.settings, 'session_secret', 'test-session-secret-at-least-32-characters')
    monkeypatch.setattr(main.settings, 'trusted_proxies', '127.0.0.1/32')
    monkeypatch.setattr(main.settings, 'admin_password', 'test-only-password')
    with TestClient(main.app) as client:
        for _ in range(5):
            assert client.post('/api/v1/auth/login', data={'username': 'operator', 'password': 'wrong'}, headers={'X-Forwarded-For': '192.0.2.1'}).status_code == 401
        assert client.post('/api/v1/auth/login', data={'username': 'operator', 'password': 'test-only-password'}, headers={'X-Forwarded-For': '192.0.2.2'}).status_code == 200
        assert main._login_backoffs[('operator', '192.0.2.1')][0] == 5
        assert ('operator', '192.0.2.2') not in main._login_backoffs
        for index in range(3, 27):
            assert client.post('/api/v1/auth/login', data={'username': 'operator', 'password': 'wrong'}, headers={'X-Forwarded-For': f'192.0.2.{index}'}).status_code == 401
        response = client.post('/api/v1/auth/login', data={'username': 'operator', 'password': 'wrong'}, headers={'X-Forwarded-For': '192.0.2.99'})
        assert response.status_code == 429
        assert response.headers['Retry-After'] == '60'
