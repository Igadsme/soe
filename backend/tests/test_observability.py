import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import observability as obs
from app.config import settings
from app.main import app


def test_metrics_disabled():
    with TestClient(app) as client:
        assert client.get('/metrics').status_code == 404


def test_metrics_token(monkeypatch):
    monkeypatch.setattr(settings, 'metrics_enabled', True)
    monkeypatch.setattr(settings, 'metrics_token', SecretStr('test-token'))
    with TestClient(app) as client:
        assert client.get('/metrics').status_code == 401
        assert client.get('/metrics', headers={'Authorization': 'Bearer wrong'}).status_code == 401
        client.get('/api/v1/sources/999/rights')
        response = client.get('/metrics', headers={'Authorization': 'Bearer test-token'})
    assert response.status_code == 200
    assert 'soe_http_requests_total' in response.text
    assert 'soe_http_request_duration_seconds' in response.text
    assert 'soe_http_requests_in_progress' in response.text
    assert 'soe_jobs{status="queued"} 0.0' in response.text
    assert 'route="/api/v1/sources/999/rights"' not in response.text
    assert 'route="/api/v1/sources/{source_id}/rights"' in response.text


@pytest.mark.parametrize('peer,status', [('127.0.0.1', 200), ('10.1.2.3', 200), ('8.8.8.8', 403), ('invalid', 403)])
def test_metrics_network(monkeypatch, peer, status):
    monkeypatch.setattr(settings, 'metrics_enabled', True)
    with TestClient(app, client=(peer, 50000)) as client:
        assert client.get('/metrics', headers={'X-Forwarded-For': '127.0.0.1'}).status_code == status


def test_sentry_no_dsn(monkeypatch):
    import sentry_sdk
    init = Mock()
    monkeypatch.setattr(sentry_sdk, 'init', init)
    monkeypatch.setattr(settings, 'sentry_dsn', SecretStr(''))
    obs.init_sentry()
    init.assert_not_called()


def test_sentry_configured(monkeypatch):
    import sentry_sdk
    init = Mock()
    monkeypatch.setattr(sentry_sdk, 'init', init)
    monkeypatch.setattr(settings, 'sentry_dsn', SecretStr('https://placeholder@example.com/1'))
    obs.init_sentry()
    kwargs = init.call_args.kwargs
    assert kwargs['send_default_pii'] is False
    assert kwargs['traces_sample_rate'] == 0
    assert kwargs['before_send'] is obs.scrub_event
    assert [item.identifier for item in kwargs['disabled_integrations']] == ['httpx']
    assert kwargs['environment'] == settings.env


def test_scrubber():
    event = {'request': {'cookies': 'secret', 'data': 'secret', 'query_string': 'secret',
                        'url': 'https://example.com/api?secret=value',
                        'headers': {'Authorization': 'secret', 'Cookie': 'secret', 'X-CSRF-Token': 'secret', 'Accept': 'json'}},
             'breadcrumbs': ['secret'], 'user': {'email': 'secret'},
             'exception': {'values': [{'stacktrace': {'frames': [{'vars': {'secret': 'secret'}}]}}]}}
    result = obs.scrub_event(event)
    assert 'secret' not in str(result)
    assert result['request']['headers'] == {'Accept': 'json'}


def test_gunicorn_dead_worker(monkeypatch):
    spec = importlib.util.spec_from_file_location('soe_gunicorn', Path('infra/api/gunicorn.conf.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    mark = Mock()
    monkeypatch.setattr(obs.multiprocess, 'mark_process_dead', mark)
    module.child_exit(None, Mock(pid=123))
    mark.assert_not_called()
    monkeypatch.setenv('PROMETHEUS_MULTIPROC_DIR', '/tmp/placeholder')
    module.child_exit(None, Mock(pid=123))
    mark.assert_called_once_with(123)
