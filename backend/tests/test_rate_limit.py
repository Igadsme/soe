"""Redis behavior without a running server; local state is cleared between replicas."""
import logging

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from app import main, rate_limit


class MemoryRedis:
    async def aclose(self): pass

    def __init__(self):
        self.now = 0
        self.windows = {}

    async def eval(self, script, numkeys, key, limit):
        assert script == rate_limit._COUNTER
        assert numkeys == 1
        assert key.startswith("soe:rl:")
        count, expiry = self.windows.get(key, (0, self.now + 60))
        if self.now >= expiry:
            count, expiry = 0, self.now + 60
        if count >= limit:
            return 1
        self.windows[key] = (count + 1, expiry)
        return 0


@pytest.fixture
def shared_redis(monkeypatch):
    monkeypatch.setattr(main.settings, "admin_password", "test-only-password")
    monkeypatch.setattr(main.settings, "admin_password_hash", "")
    backend = MemoryRedis()
    monkeypatch.setattr(main.settings, "redis_url", "redis://redis:6379/0")
    monkeypatch.setattr(rate_limit.AsyncRedis, "from_url", lambda *a, **k: backend)
    return backend


def test_shared_general_limit_and_expiration(shared_redis, monkeypatch):
    monkeypatch.setattr(main.settings, "rate_limit_per_minute", 2)
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        main._rate_windows.clear()  # Another worker has no local counts.
        assert client.get("/health").status_code == 200
        shared_redis.now = 59
        response = client.get("/health")
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"
        shared_redis.now = 60
        assert client.get("/health").status_code == 200


def test_sixth_login_is_limited_across_workers(shared_redis):
    with TestClient(main.app) as client:
        for index in range(6):
            main._login_windows.clear()
            response = client.post("/api/v1/auth/login", data={"username": "Operator" if index % 2 else "operator", "password": "bad"})
            assert response.status_code == (401 if index < 5 else 429)
        assert response.json()["detail"] == "login temporarily unavailable"
        shared_redis.now = 60
        main._login_backoffs.clear()  # A different worker has no local backoff state.
        assert client.post("/api/v1/auth/login", data={"username": "operator", "password": "bad"}).status_code == 401


def test_login_ip_limit_across_usernames(shared_redis):
    with TestClient(main.app) as client:
        for index in range(21):
            main._login_windows.clear()
            response = client.post("/api/v1/auth/login", data={"username": f"user-{index}", "password": "bad"})
            assert response.status_code == (401 if index < 20 else 429)


@pytest.mark.asyncio
async def test_scopes_and_keys_are_independent(shared_redis):
    assert not await rate_limit.async_redis_limited("", "general", "same", 1)
    assert await rate_limit.async_redis_limited("", "general", "same", 1)
    assert not await rate_limit.async_redis_limited("", "login", "same", 1)
    assert not await rate_limit.async_redis_limited("", "general", "other", 1)
    assert all("same" not in key and "other" not in key for key in shared_redis.windows)


def test_redis_outage_policy_and_safe_warning(monkeypatch, caplog):
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    monkeypatch.setattr(main.settings, "redis_url", "redis://private-test-value@redis/0")

    def unavailable(*args, **kwargs):
        raise ConnectionError("private-test-value")

    monkeypatch.setattr(rate_limit.AsyncRedis, "from_url", unavailable)
    with caplog.at_level(logging.WARNING, logger="soe.rate_limit"), TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        response = client.post("/api/v1/auth/login", data={"username": "operator", "password": "bad"})
    assert response.status_code == 503
    assert response.json() == {"detail": "login temporarily unavailable"}
    assert response.headers["Retry-After"] == "60"
    assert "Redis rate limiter unavailable" in caplog.text
    assert "private-test-value" not in caplog.text
    assert "private-test-value" not in repr(main.settings)


def test_empty_url_uses_local_windows(monkeypatch):
    monkeypatch.setattr(main.settings, "redis_url", "")

    def unexpected(url):
        pytest.fail("Redis must not be used without a URL")

    monkeypatch.setattr(main, "async_redis_limited", unexpected)
    for _ in range(5):
        assert not main.limited(main._login_windows, "test", 5)
    assert main.limited(main._login_windows, "test", 5)


@pytest.mark.asyncio
async def test_redis_calls_run_off_event_loop(monkeypatch):
    import threading
    from httpx import ASGITransport, AsyncClient
    loop_thread = threading.get_ident()
    calls = []
    async def limited(*args):
        calls.append(threading.get_ident())
        assert threading.get_ident() == loop_thread
        return False
    monkeypatch.setattr(main, 'async_redis_limited', limited)
    monkeypatch.setattr(main.settings, 'redis_url', 'redis://test-only/0')
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url='http://test') as client:
        assert (await client.get('/health')).status_code == 200
        assert (await client.post('/api/v1/auth/login', data={'username':'operator','password':'test'})).status_code == 200
    assert len(calls) == 4  # General requests plus both login limits.
