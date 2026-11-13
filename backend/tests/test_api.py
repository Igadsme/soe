import pytest
from httpx import ASGITransport, AsyncClient
from app.db import Base, engine
from app.main import app

Base.metadata.create_all(engine)


@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_discovery_fixture_is_persisted():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/discovery/search?platform=youtube&query=test")
    assert response.status_code == 200
    assert response.json()["fixture"] is True
