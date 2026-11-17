import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import main
from app.models import Clip, LedgerEntry, MetricEvent, Publication, RightsRecord, Source


def test_panels_and_publish_rights(isolated_database, monkeypatch):
    monkeypatch.setattr(main.settings, 'publishing_enabled', True)
    with isolated_database() as db:
        source = Source(platform='upload', external_id='panels', title='Title', url='https://example.com')
        db.add(source)
        db.flush()
        clip = Clip(source_id=source.id, start_seconds=0, end_seconds=10, description='Test', hook='Hook', score=90)
        db.add(clip)
        db.flush()
        clip_id, source_id = clip.id, source.id
        db.add_all([MetricEvent(publication_id=1, metric='views', value=10, verified=True), MetricEvent(publication_id=1, metric='likes', value=2, verified=False), LedgerEntry(kind='revenue', category='ads', cents=100, verified=True, note='verified'), LedgerEntry(kind='expense', category='compute', cents=5, note='expense'), Publication(clip_id=clip.id, platform='youtube', error='private provider error')])
        db.commit()
    with TestClient(main.app) as client:
        assert client.post('/api/v1/clips/999/publish?platform=youtube').status_code == 404
        assert client.post(f'/api/v1/clips/{clip_id}/publish?platform=youtube').status_code == 403
        with isolated_database() as db:
            db.add(RightsRecord(source_id=source_id, authorization_type='owned', commercial_use=True, publishing_allowed=True, allowed_platforms=['youtube'], evidence='test rights'))
            db.commit()
        assert client.post(f'/api/v1/clips/{clip_id}/publish?platform=tiktok').status_code == 403
        assert client.post(f'/api/v1/clips/{clip_id}/publish?platform=youtube').json()['status'] == 'queued'
        assert client.get('/api/v1/publications').json()[-1]['error'] == 'publication failed'
        analytics = client.get('/api/v1/analytics').json()
        assert analytics['verified'][0]['value'] == 10 and analytics['unverified'][0]['value'] == 2
        assert len(client.get('/api/v1/revenue').json()['entries']) == 2
        assert client.get('/api/v1/overview').json()['counts']['clips'] == 1
        assert client.get('/api/v1/settings').json()['publishing_enabled']
        assert client.get('/api/v1/agents').status_code == 200
        monkeypatch.setattr(main.settings, 'publishing_enabled', False)
        assert client.post(f'/api/v1/clips/{clip_id}/publish?platform=youtube').status_code == 409


def test_discovery_status_and_unavailable(monkeypatch):
    monkeypatch.setattr(main.settings, 'youtube_api_key', '')
    monkeypatch.setattr(main.settings, 'twitch_client_id', '')
    monkeypatch.setattr(main.settings, 'twitch_client_secret', '')
    with TestClient(main.app) as client:
        assert client.get('/api/v1/agents').json()[0]['detail'] == 'fixture mode (development)'
        # Keep auth override to inspect non-demo provider behavior.
        main.app.dependency_overrides[main.require_auth] = lambda: 'operator'
        monkeypatch.setattr(main.settings, 'demo_mode', False)
        assert client.get('/api/v1/agents').json()[0]['detail'] == 'blocked: no provider credentials configured'
        assert client.post('/api/v1/discovery/search?platform=youtube&query=test').status_code == 503
        assert client.post('/api/v1/discovery/search?platform=twitch&query=test').status_code == 503
        monkeypatch.setattr(main.settings, 'youtube_api_key', 'test-key')
        assert client.get('/api/v1/agents').json()[0]['detail'] == 'official adapters configured'


def test_ready_independent_of_cwd(isolated_database, tmp_path, monkeypatch):
    with isolated_database() as db:
        db.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32))'))
        db.execute(text("INSERT INTO alembic_version VALUES ('0005_security')"))
        db.commit()
    monkeypatch.chdir(tmp_path)
    with TestClient(main.app) as client:
        assert client.get('/ready').status_code == 200


def test_dev_login_demo_and_rights_missing():
    with TestClient(main.app) as client:
        assert client.post('/api/v1/auth/login', data={'username': 'operator', 'password': 'anything'}).json()['mode'] == 'development-bypass'
        assert client.post('/api/v1/auth/logout').status_code == 200
        assert client.post('/api/v1/demo/pipeline').status_code == 200
        assert client.post('/api/v1/sources/999/rights', json={'authorization_type':'owned', 'commercial_use':True, 'publishing_allowed':True, 'allowed_platforms':['youtube'], 'evidence':'Owned media authorization evidence'}).status_code == 404
