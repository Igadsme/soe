from types import SimpleNamespace
import pytest
from httpx import ASGITransport, AsyncClient
from fastapi import HTTPException
from app import auth
from app.main import app


def test_signed_session_rejects_tampering(monkeypatch):
    monkeypatch.setattr(auth.settings, "session_secret", "test-secret")
    monkeypatch.setattr(auth.settings, "session_ttl_seconds", 60)
    token = auth.make_session("operator")
    assert auth.verify_session(token) == "operator"
    with pytest.raises(HTTPException) as error:
        auth.verify_session(token[:-2] + "xx")
    assert error.value.status_code == 401
    stored = auth.make_password_hash("correct horse")
    assert auth.verify_password("correct horse", stored)
    assert not auth.verify_password("wrong horse", stored)


def test_signed_sessions_are_stable_across_many_signings(monkeypatch):
    # Regression: the old format split the raw HMAC digest on ".", so ~12% of
    # tokens failed to verify. Every valid token must verify and every
    # tampered token must be rejected.
    monkeypatch.setattr(auth.settings, "session_secret", "test-secret")
    monkeypatch.setattr(auth.settings, "session_ttl_seconds", 60)
    for i in range(2000):
        username = f"operator-{i}.name:with:colons"
        token = auth.make_session(username)
        assert auth.verify_session(token) == username
        payload_b64, signature = token.split(".")
        flipped = "0" if signature[-1] != "0" else "1"
        tampered = [
            f"{payload_b64}.{signature[:-1]}{flipped}",
            f"{payload_b64[:-1]}{'A' if payload_b64[-1] != 'A' else 'B'}.{signature}",
            f"{payload_b64}.{signature}.extra",
            payload_b64,
            "",
        ]
        for bad in tampered:
            with pytest.raises(HTTPException) as error:
                auth.verify_session(bad)
            assert error.value.status_code == 401


def test_session_signed_with_other_secret_is_rejected(monkeypatch):
    monkeypatch.setattr(auth.settings, "session_ttl_seconds", 60)
    monkeypatch.setattr(auth.settings, "session_secret", "secret-a")
    token = auth.make_session("operator")
    monkeypatch.setattr(auth.settings, "session_secret", "secret-b")
    with pytest.raises(HTTPException):
        auth.verify_session(token)


def test_expired_session_is_rejected(monkeypatch):
    monkeypatch.setattr(auth.settings, "session_secret", "test-secret")
    monkeypatch.setattr(auth.settings, "session_ttl_seconds", -1)
    token = auth.make_session("operator")
    with pytest.raises(HTTPException):
        auth.verify_session(token)


@pytest.mark.asyncio
async def test_production_overview_requires_session(monkeypatch):
    monkeypatch.setattr(auth.settings, "env", "production")
    monkeypatch.setattr(auth.settings, "demo_mode", False)
    monkeypatch.setattr(auth.settings, "session_secret", "test-secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/overview")
    assert response.status_code == 401
