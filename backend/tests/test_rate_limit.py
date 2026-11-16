"""Redis behavior without a running server; local state is cleared between replicas."""
import logging

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from app import main, rate_limit


class MemoryRedis:
    def __init__(self):
        self.now = 0
        self.windows = {}

    def eval(self, script, numkeys, key, limit):
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
    monkeypatch.setattr(rate_limit, "redis_client", lambda url: backend)
    monkeypatch.setattr(main.settings, "redis_url", "redis://redis:6379/0")
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
        assert client.post("/api/v1/auth/login", data={"username": "operator", "password": "bad"}).status_code == 401


def test_login_ip_limit_across_usernames(shared_redis):
    with TestClient(main.app) as client:
        for index in range(21):
            main._login_windows.clear()
            response = client.post("/api/v1/auth/login", data={"username": f"user-{index}", "password": "bad"})
            assert response.status_code == (401 if index < 20 else 429)


def test_scopes_and_keys_are_independent(shared_redis):
    assert not rate_limit.redis_limited("", "general", "same", 1)
    assert rate_limit.redis_limited("", "general", "same", 1)
    assert not rate_limit.redis_limited("", "login", "same", 1)
    assert not rate_limit.redis_limited("", "general", "other", 1)
    assert all("same" not in key and "other" not in key for key in shared_redis.windows)


def test_redis_outage_policy_and_safe_warning(monkeypatch, caplog):
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    monkeypatch.setattr(main.settings, "redis_url", "redis://private-test-value@redis/0")

    def unavailable(url):
        raise ConnectionError("private-test-value")

    monkeypatch.setattr(rate_limit, "redis_client", unavailable)
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

    monkeypatch.setattr(rate_limit, "redis_client", unexpected)
    for _ in range(5):
        assert not main.limited(main._login_windows, "test", 5)
    assert main.limited(main._login_windows, "test", 5)
