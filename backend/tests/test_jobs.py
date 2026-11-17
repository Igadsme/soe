from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main, worker
from app.models import AuthSession, Clip, Job, RightsRecord, Source, WorkflowRun, now
from app.media import MediaProbe


@pytest.fixture
def queued(isolated_database, tmp_path):
    media = Path(main.settings.media_root) / 'input.mp4'
    media.parent.mkdir(parents=True)
    media.write_bytes(b'video')
    with isolated_database() as db:
        source = Source(platform='upload', external_id='job-source', title='test', url='https://example.com', media_path=str(media))
        db.add(source)
        db.flush()
        db.add(RightsRecord(source_id=source.id, authorization_type='owned', commercial_use=True, publishing_allowed=True, evidence='owned media'))
        db.commit()
        return source.id


def enqueue(source_id, key=None):
    with TestClient(main.app) as client:
        response = client.post(f'/api/v1/sources/{source_id}/process', headers={'Idempotency-Key': key} if key else {})
    assert response.status_code in (200, 202), response.text
    return response.json()['job_id']


def test_enqueue_poll_and_privacy(queued, isolated_database):
    job_id = enqueue(queued, 'same-request')
    assert enqueue(queued) == job_id
    assert enqueue(queued, 'same-request') == job_id
    with TestClient(main.app) as client:
        job = client.get(f'/api/v1/jobs/{job_id}').json()
        assert job['status'] == 'queued' and job['attempts'] == 0
        assert client.get('/api/v1/jobs').json()[0]['id'] == job_id
        assert client.get('/api/v1/jobs/missing').status_code == 404
        source = client.get('/api/v1/sources').json()[0]
        assert source['has_media'] and 'media_path' not in source
        assert client.post(f'/api/v1/sources/{queued}/process', headers={'Idempotency-Key': 'a'*256}).status_code == 422
    with isolated_database() as db:
        assert len(list(db.scalars(select(WorkflowRun)))) == 1


def test_concurrent_claim_and_lease_reclaim(queued, isolated_database):
    job_id = enqueue(queued)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(worker.claim_job, ['one', 'two']))
    assert results.count(job_id) == 1 and results.count(None) == 1
    with isolated_database() as db:
        job = db.get(Job, job_id)
        previous_owner = job.locked_by
        job.lease_expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert worker.claim_job('recovered') == job_id
    worker.fail_job(job_id, previous_owner)
    with isolated_database() as db:
        job = db.get(Job, job_id)
        assert job.attempts == 2 and job.locked_by == 'recovered'
    worker.renew_lease(job_id, 'recovered')
    worker.fail_job(job_id, 'recovered')
    with isolated_database() as db:
        job = db.get(Job, job_id)
        assert job.status == 'queued' and job.last_error == 'media processing failed'
        assert job.run_after > now().replace(tzinfo=None)
        job.run_after = now() - timedelta(seconds=1)
        db.commit()
    assert worker.claim_job('last') == job_id
    worker.fail_job(job_id, 'last')
    assert worker.claim_job('extra') is None
    with isolated_database() as db:
        assert db.get(Job, job_id).status == 'failed'
        assert db.get(WorkflowRun, db.get(Job, job_id).workflow_id).state == 'failed'


def test_crash_at_last_attempt(queued, isolated_database):
    job_id = enqueue(queued)
    worker.claim_job('crashed')
    with isolated_database() as db:
        job = db.get(Job, job_id)
        job.attempts = 3
        job.lease_expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert worker.claim_job('new') is None
    with isolated_database() as db:
        assert db.get(Job, job_id).status == 'failed'


def stub_media(monkeypatch):
    def ingest(source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b'media')
        return destination, 'digest', MediaProbe(30, 100, 100, True)
    monkeypatch.setattr(worker, 'ingest_authorized', ingest)
    monkeypatch.setattr(worker, 'FasterWhisperTranscriber', lambda _: SimpleNamespace(transcribe=lambda path: ['segment']))
    monkeypatch.setattr(worker, 'extract_candidates', lambda *a, **k: [SimpleNamespace(start=0, end=5, description='words', hook='hook', score=90, evidence={})])
    monkeypatch.setattr(worker, 'render_vertical', lambda *args: args[1].write_bytes(b'output'))


def test_processing_idempotence_and_fencing(queued, isolated_database, monkeypatch):
    stub_media(monkeypatch)
    job_id = enqueue(queued, 'completed-key')
    worker.claim_job('owner')
    worker.process_job(job_id, 'stale')
    worker.process_job(job_id, 'owner')
    worker.process_job(job_id, 'owner')
    with isolated_database() as db:
        assert db.get(Job, job_id).status == 'succeeded'
        assert db.get(Job, job_id).result['clips_generated'] == 1
        assert len(list(db.scalars(select(Clip)))) == 1
        assert db.get(Source, queued).media_sha256 == 'digest'
    assert enqueue(queued, 'completed-key') == job_id
    with TestClient(main.app) as client:
        clips = client.get('/api/v1/clips').json()
        assert len(clips) == 1
        assert client.get(f"/api/v1/clips/{clips[0]['id']}/download").content == b'output'
        assert client.get('/api/v1/clips/999/download').status_code == 404


@pytest.mark.asyncio
async def test_process_once_failure_then_success(queued, isolated_database, monkeypatch):
    job_id = enqueue(queued)
    real_process_job = worker.process_job
    monkeypatch.setattr(worker, 'process_job', lambda *a: (_ for _ in ()).throw(RuntimeError('secret')))
    assert await worker.process_once() == 1
    assert await worker.process_once() == 0
    with isolated_database() as db:
        job = db.get(Job, job_id)
        assert 'secret' not in job.last_error
        job.run_after = now() - timedelta(seconds=1)
        db.commit()
    monkeypatch.setattr(worker, 'process_job', real_process_job)
    stub_media(monkeypatch)
    assert await worker.process_once() == 1


def test_session_cleanup(isolated_database, monkeypatch):
    monkeypatch.setattr(worker.settings, 'session_retention_seconds', 100)
    timestamp = int(time.time())
    with isolated_database() as db:
        for name, expires, revoked in [('old', timestamp-101, None), ('recent', timestamp-99, None), ('revoked', timestamp+1000, timestamp-101), ('active', timestamp+1000, None)]:
            db.add(AuthSession(id=name, username='operator', expires_at=expires, revoked_at=revoked))
        db.commit()
    assert worker.cleanup_sessions() == 2
    assert worker.cleanup_sessions() == 0
    with isolated_database() as db:
        assert set(db.scalars(select(AuthSession.id))) == {'recent', 'active'}


def test_process_rights_paths(queued, isolated_database):
    with TestClient(main.app) as client:
        assert client.post('/api/v1/sources/999/process').status_code == 404
        with isolated_database() as db:
            source = db.get(Source, queued)
            source.media_path = '/outside/root'
            db.commit()
        assert client.post(f'/api/v1/sources/{queued}/process').status_code == 422
        with isolated_database() as db:
            rights = db.scalar(select(RightsRecord))
            rights.revoked = True
            db.commit()
        assert client.post(f'/api/v1/sources/{queued}/process').status_code == 403


@pytest.mark.asyncio
async def test_worker_loop_shutdown_and_cleanup(monkeypatch):
    calls = []
    handlers = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, 'add_signal_handler', lambda sig, fn: handlers.__setitem__(sig, fn))
    monkeypatch.setattr(loop, 'remove_signal_handler', lambda sig: handlers.pop(sig))
    monkeypatch.setattr(type(worker.settings), 'validate_production', lambda self: calls.append('validated'))
    monkeypatch.setattr(worker, 'cleanup_sessions', lambda: calls.append('cleanup'))
    monkeypatch.setattr(worker.Path, 'touch', lambda self: calls.append('heartbeat'))
    async def process():
        calls.append('process')
        next(iter(handlers.values()))()
        return 0
    monkeypatch.setattr(worker, 'process_once', process)
    await worker.run_forever(interval_seconds=0)
    assert calls == ['validated', 'cleanup', 'process', 'heartbeat']
    assert not handlers


def test_lost_lease_cannot_commit_outputs(queued, isolated_database, monkeypatch):
    stub_media(monkeypatch)
    job_id = enqueue(queued)
    worker.claim_job('old')
    def reclaim(*args):
        with isolated_database() as db:
            job = db.get(Job, job_id)
            job.lease_expires_at = now() - timedelta(seconds=1)
            db.commit()
        assert worker.claim_job('new') == job_id
    monkeypatch.setattr(worker, 'render_vertical', reclaim)
    worker.process_job(job_id, 'old')
    with isolated_database() as db:
        assert db.get(Job, job_id).locked_by == 'new'
        assert not list(db.scalars(select(Clip)))


def test_worker_rechecks_rights(queued, isolated_database):
    job_id = enqueue(queued)
    worker.claim_job('owner')
    with isolated_database() as db:
        db.scalar(select(RightsRecord)).revoked = True
        db.commit()
    with pytest.raises(ValueError, match='authorization'):
        worker.process_job(job_id, 'owner')


def test_concurrent_enqueue(queued):
    with TestClient(main.app) as client:
        def post(_):
            response = client.post(f'/api/v1/sources/{queued}/process', headers={'Idempotency-Key': 'concurrent'})
            assert response.status_code in (200, 202), response.text
            return response.json()['job_id']
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(post, range(2)))
    assert results[0] == results[1]


def test_postgres_claim_uses_skip_locked(queued, isolated_database, monkeypatch):
    from sqlalchemy.dialects import postgresql
    job_id = enqueue(queued)
    queries = []
    class PostgresClaimSession:
        bind = SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))
        def __enter__(self):
            self.db = isolated_database()
            return self
        def __exit__(self, *args): self.db.close()
        def scalars(self, query): return self.db.scalars(query)
        def scalar(self, query):
            queries.append(str(query.compile(dialect=postgresql.dialect())))
            return self.db.scalar(query)
        def execute(self, query): return self.db.execute(query)
        def commit(self): self.db.commit()
    monkeypatch.setattr(worker, 'SessionLocal', PostgresClaimSession)
    assert worker.claim_job('postgres-owner') == job_id
    assert 'FOR UPDATE SKIP LOCKED' in queries[0]


@pytest.mark.asyncio
async def test_process_once_renews_during_long_work(queued, monkeypatch):
    enqueue(queued)
    renewals = []
    original_renew = worker.renew_lease
    def renew(*args):
        renewals.append(args)
        original_renew(*args)
    monkeypatch.setattr(worker, 'LEASE_SECONDS', 0.12)
    monkeypatch.setattr(worker, 'renew_lease', renew)
    monkeypatch.setattr(worker.Path, 'touch', lambda self: None)
    def slow(*args): time.sleep(0.15)
    monkeypatch.setattr(worker, 'process_job', slow)
    assert await worker.process_once() == 1
    assert renewals


def test_idempotency_key_cannot_cross_sources(queued):
    enqueue(queued, 'reserved')
    with TestClient(main.app) as client:
        path = str(Path(main.settings.media_root) / 'input.mp4')
        response = client.post('/api/v1/sources', json={'platform': 'upload', 'external_id': 'second', 'title': 'Second', 'url': 'https://example.com', 'media_path': path})
        assert response.status_code == 201
        source_id = response.json()['id']
        response = client.post(f'/api/v1/sources/{source_id}/rights', json={'authorization_type':'owned', 'commercial_use':True, 'publishing_allowed':True, 'evidence':'owned media'})
        assert response.status_code == 201 and response.json()['status'] == 'authorized'
        assert client.post(f'/api/v1/sources/{source_id}/process', headers={'Idempotency-Key':'reserved'}).status_code == 409
