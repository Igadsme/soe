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


@pytest.mark.asyncio
async def test_production_overview_requires_session(monkeypatch):
    monkeypatch.setattr(auth.settings, "env", "production")
    monkeypatch.setattr(auth.settings, "demo_mode", False)
    monkeypatch.setattr(auth.settings, "session_secret", "test-secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/overview")
    assert response.status_code == 401
