"""Adversarial checks across every route: authentication, CSRF, secrets in URLs, SSRF, CORS."""
import re
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app import auth, integrations, main, publishing
from app.config import settings

PUBLIC = {("POST", "/api/v1/auth/login"), ("GET", "/api/v1/oauth/{platform}/callback")}
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def _flatten(routes):
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:  # FastAPI wraps include_router() entries
            yield from _flatten(included.routes)
        else:
            yield route


def api_routes():
    for route in _flatten(main.app.routes):
        if isinstance(route, APIRoute) and route.path.startswith("/api/"):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                yield method, route.path


def concrete(path):
    return re.sub(r"\{[^}]+\}", lambda m: "youtube" if m.group(0) == "{platform}" else "1", path)


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setattr(auth.settings, "env", "production")
    monkeypatch.setattr(auth.settings, "demo_mode", False)
    monkeypatch.setattr(auth.settings, "dev_auth_bypass", False)
    monkeypatch.setattr(auth.settings, "session_secret", SecretStr("adversarial-session-key" * 3))
    monkeypatch.setattr(auth.settings, "admin_password", SecretStr(""))
    monkeypatch.setattr(auth.settings, "admin_password_hash", SecretStr(auth.make_password_hash("test-password")))
    monkeypatch.setattr(auth.settings, "rate_limit_per_minute", 100000)
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="https://test") as value:
        yield value


def test_route_inventory_is_nonempty():
    routes = set(api_routes())
    assert len(routes) > 30
    assert PUBLIC <= routes


@pytest.mark.asyncio
async def test_every_api_route_requires_authentication(client):
    failures = []
    for method, path in sorted(api_routes()):
        if (method, path) in PUBLIC:
            continue
        response = await client.request(method, concrete(path), json={})
        if response.status_code != 401:
            failures.append((method, path, response.status_code))
    assert failures == []


@pytest.mark.asyncio
async def test_every_mutating_route_requires_csrf(client):
    login = await client.post("/api/v1/auth/login", data={"username": "operator", "password": "test-password"})
    assert login.status_code == 200
    failures = []
    for method, path in sorted(api_routes()):
        if method not in MUTATING or (method, path) in PUBLIC:
            continue
        for headers in ({}, {"X-CSRF-Token": "forged"}):
            response = await client.request(method, concrete(path), json={}, headers=headers)
            if response.status_code != 403:
                failures.append((method, path, headers, response.status_code))
    assert failures == []


@pytest.mark.asyncio
async def test_disallowed_origin_gets_no_cors_grant(client):
    response = await client.options("/api/v1/auth/me", headers={"Origin": "https://evil.example",
                                                                "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in response.headers
    allowed = settings.cors_origins.split(",")[0].strip()
    ok = await client.options("/api/v1/auth/me", headers={"Origin": allowed, "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == allowed


def recorder(monkeypatch, responses):
    seen = []

    async def handler(request):
        seen.append(request)
        return responses(request)

    def factory(*args, **kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(integrations, "metadata_client", factory)
    return seen


@pytest.mark.asyncio
async def test_youtube_api_key_never_in_url(monkeypatch):
    monkeypatch.setattr(settings, "youtube_api_key", SecretStr("yt-key-fixture"))

    def respond(request):
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"items": [{"id": {"videoId": "v1"}, "snippet": {"title": "T"}}]})
        return httpx.Response(200, json={"items": []})

    seen = recorder(monkeypatch, respond)
    await integrations.YouTubeDiscovery().discover("pasta", [], 5)
    await integrations.YouTubeDiscovery().discover("pasta", ["UCchannel"], 5)
    assert seen
    for request in seen:
        assert "yt-key-fixture" not in str(request.url)
        assert request.headers["x-goog-api-key"] == "yt-key-fixture"


@pytest.mark.asyncio
async def test_twitch_secret_sent_in_body_not_url(monkeypatch):
    monkeypatch.setattr(settings, "twitch_client_id", "twitch-id")
    monkeypatch.setattr(settings, "twitch_client_secret", SecretStr("twitch-secret-fixture"))

    def respond(request):
        if request.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "app-token"})
        return httpx.Response(200, json={"data": []})

    seen = recorder(monkeypatch, respond)
    await integrations.TwitchDiscovery().discover("speedrun", [], 5)
    token = seen[0]
    assert "twitch-secret-fixture" not in str(token.url)
    assert b"client_secret=twitch-secret-fixture" in token.content
    assert all("app-token" not in str(request.url) for request in seen)


@pytest.mark.parametrize("url", [
    "http://www.googleapis.com/upload", "https://169.254.169.254/latest/meta-data", "file:///etc/passwd",
    "https://www.googleapis.com.evil.example/upload", "https://user@www.googleapis.com/upload",
    "https://www.googleapis.com:8443/upload", "https://127.0.0.1/upload", None, "",
])
def test_provider_upload_urls_are_allowlisted(url):
    with pytest.raises(publishing.PublishingUnavailable):
        publishing.upload_url(url, {"www.googleapis.com"})
    assert publishing.upload_url("https://www.googleapis.com/upload?x=1", {"www.googleapis.com"})


def test_no_secret_literals_in_app_or_infra():
    root = Path(__file__).resolve().parents[2]
    pattern = re.compile(r"(AIza[0-9A-Za-z_-]{30,}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY)")
    hits = [str(path) for base in ("backend/app", "infra", "frontend/app") for path in (root / base).rglob("*")
            if path.is_file() and path.suffix in {".py", ".ts", ".tsx", ".yml", ".yaml", ".toml", ".sh", ""}
            and pattern.search(path.read_text(errors="ignore"))]
    assert hits == []


def test_subprocess_calls_have_timeouts():
    root = Path(__file__).resolve().parents[2] / "backend" / "app"
    offenders = []
    for path in root.rglob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"subprocess\.(run|call|check_output|check_call)\(", line) and "timeout" not in line:
                offenders.append(f"{path.name}:{number}")
            if "shell=True" in line:
                offenders.append(f"{path.name}:{number} shell=True")
    assert offenders == []
