import json
import logging
import re
import time
from collections import defaultdict, deque

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text
from starlette.requests import Request

from app import auth, main
from app.config import Settings
from app.db import session
from app.logging_config import JsonFormatter
from app.models import AuthSession, Source, RightsRecord


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setattr(auth.settings, "env", "production")
    monkeypatch.setattr(auth.settings, "demo_mode", False)
    monkeypatch.setattr(auth.settings, "session_secret", SecretStr("test-session-key" * 4))
    monkeypatch.setattr(auth.settings, "admin_password", SecretStr(""))
    monkeypatch.setattr(auth.settings, "admin_password_hash", SecretStr(auth.make_password_hash("test-password")))
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="https://test") as value:
        yield value


async def sign_in(client):
    response = await client.post("/api/v1/auth/login", data={"username": "operator", "password": "test-password"})
    assert response.status_code == 200
    return {"X-CSRF-Token": client.cookies["soe_csrf"]}


@pytest.mark.asyncio
async def test_session_csrf_and_logout_revocation(client):
    headers = await sign_in(client)
    token = client.cookies["soe_session"]
    assert (await client.get("/api/v1/auth/me")).json() == {"username": "operator"}
    assert (await client.post("/api/v1/auth/logout")).status_code == 403
    assert (await client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "wrong"})).status_code == 403
    response = await client.post("/api/v1/auth/logout", headers=headers)
    assert response.status_code == 200
    for cookie in response.headers.get_list("set-cookie"):
        assert "Max-Age=0" in cookie and "Path=/" in cookie and "SameSite=lax" in cookie and "Secure" in cookie
    with pytest.raises(HTTPException):
        auth.verify_session(token)
    client.cookies.set("soe_session", token)
    assert (await client.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_login_cookie_flags_and_expiry(client, isolated_database):
    response = await client.post("/api/v1/auth/login", data={"username": "operator", "password": "test-password"})
    cookies = response.headers.get_list("set-cookie")
    assert all("Secure" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie for cookie in cookies)
    assert "HttpOnly" in cookies[0] and "HttpOnly" not in cookies[1]
    token = client.cookies["soe_session"]
    with isolated_database() as db:
        row = db.query(AuthSession).one()
        row.expires_at = int(time.time()) - 1
        db.commit()
    with pytest.raises(HTTPException):
        auth.verify_session(token)


@pytest.mark.asyncio
async def test_brute_force_limit_and_generic_error(client):
    for index in range(5):
        response = await client.post("/api/v1/auth/login", data={"username": "operator", "password": "wrong"}, headers={"X-Forwarded-For": f"192.0.2.{index}"})
        assert response.status_code == 401 and response.json()["detail"] == "invalid credentials"
    response = await client.post("/api/v1/auth/login", data={"username": "operator", "password": "test-password"})
    assert response.status_code == 429 and int(response.headers["Retry-After"]) > 0


@pytest.mark.asyncio
async def test_all_routes_require_auth(client):
    public = {"/health", "/ready", "/api/v1/auth/login"}
    for route in main.app.routes:
        if route.path == "/metrics":
            assert (await client.get("/metrics")).status_code == 404
            continue
        if route.path in public:
            continue
        url = re.sub(r"\{[^}]+\}", "1", route.path)
        for method in route.methods:
            response = await client.request(method, url)
            assert response.status_code == 401, (method, url, response.text)


@pytest.mark.asyncio
async def test_all_mutations_require_csrf(client):
    await sign_in(client)
    for route in main.app.routes:
        if route.path == "/api/v1/auth/login":
            continue
        for method in route.methods & {"POST", "PUT", "PATCH", "DELETE"}:
            url = re.sub(r"\{[^}]+\}", "1", route.path)
            assert (await client.request(method, url)).status_code == 403, url


@pytest.mark.asyncio
async def test_validation_and_demo_disabled(client, tmp_path, monkeypatch):
    headers = await sign_in(client)
    source = {"platform": "upload", "external_id": "security-test", "title": "Test", "url": "https://example.com/video", "media_path": "/etc/passwd"}
    assert (await client.post("/api/v1/sources", json=source, headers=headers)).status_code == 422
    root = tmp_path / "media"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path)
    source["media_path"] = str(root / "escape" / "outside.mp4")
    assert (await client.post("/api/v1/sources", json=source, headers=headers)).status_code == 422
    source["media_path"] = str(root / "valid.mp4")
    assert (await client.post("/api/v1/sources", json=source, headers=headers)).status_code == 201
    source["title"] = "x" * 501
    assert (await client.post("/api/v1/sources", json=source, headers=headers)).status_code == 422
    for query in ["platform=bad&query=test", "platform=youtube&query=", "platform=youtube&query=" + "a" * 201]:
        assert (await client.post("/api/v1/discovery/search?" + query, headers=headers)).status_code == 422
    assert (await client.get("/api/v1/clips/-1/download")).status_code == 422
    assert (await client.post("/api/v1/demo/pipeline", headers=headers)).status_code == 404
    monkeypatch.setattr(auth.settings, "demo_mode", True)
    assert (await client.post("/api/v1/demo/pipeline", headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_upload_signature_size_and_sanitization(client, monkeypatch, tmp_path):
    headers = await sign_in(client)
    path = "/api/v1/sources/upload"
    for data, content_type in [(b"not a video", "video/mp4"), (b"\x1a\x45\xdf\xa3", "video/mp4"), (b"junk", "text/plain")]:
        response = await client.post(path, data={"title": "test"}, files={"file": ("../../evil.mp4", data, content_type)}, headers=headers)
        assert response.status_code == 415
    valid = b"\x00\x00\x00\x18ftypisom" + b"x" * 50
    response = await client.post(path, data={"title": "test"}, files={"file": ("../../evil.mp4", valid, "video/mp4")}, headers=headers)
    assert response.status_code == 200
    assert re.fullmatch(r"upload-[a-f0-9]+.mp4", response.json()["filename"])
    monkeypatch.setattr(auth.settings, "max_upload_bytes", 10)
    assert (await client.post(path, data={"title": "test"}, files={"file": ("test.mp4", valid, "video/mp4")}, headers=headers)).status_code == 413
    assert len(list((tmp_path / "media").iterdir())) == 1


@pytest.mark.asyncio
async def test_security_headers_request_id_and_generic_500(client):
    response = await client.get("/health", headers={"X-Request-ID": "test-request-123"})
    assert response.json() == {"status": "ok", "service": "soe-api"}
    assert response.headers["X-Request-ID"] == "test-request-123"
    for name in ["Strict-Transport-Security", "Permissions-Policy", "Cross-Origin-Opener-Policy", "Content-Security-Policy", "X-Content-Type-Options"]:
        assert name in response.headers
    await sign_in(client)

    def broken_db():
        raise RuntimeError("private secret exception")

    main.app.dependency_overrides[session] = broken_db
    response = await client.get("/api/v1/overview", headers={"X-Request-ID": "error-test"})
    assert response.status_code == 500
    assert response.json() == {"detail": "internal server error", "request_id": "error-test"}
    assert response.headers["X-Request-ID"] == "error-test"
    assert "private" not in response.text
    assert (await client.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_readiness_requires_migration_head(client, isolated_database):
    assert (await client.get("/ready")).status_code == 503
    with isolated_database() as db:
        db.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        db.execute(text("INSERT INTO alembic_version VALUES ('0005_security')"))
        db.commit()
    assert (await client.get("/ready")).json() == {"status": "ready", "service": "soe-api"}
    with isolated_database() as db:
        db.execute(text("UPDATE alembic_version SET version_num='0002_clip_outputs'"))
        db.commit()
    assert (await client.get("/ready")).status_code == 503


def test_proxy_trust_and_bounded_limiter(monkeypatch):
    request = Request({"type": "http", "client": ("127.0.0.1", 1234), "headers": [(b"x-forwarded-for", b"192.0.2.1")]})
    monkeypatch.setattr(auth.settings, "trusted_proxies", "")
    assert main.client_ip(request) == "127.0.0.1"
    monkeypatch.setattr(auth.settings, "trusted_proxies", "127.0.0.1/32")
    assert main.client_ip(request) == "192.0.2.1"
    windows = defaultdict(deque, {str(i): deque([time.monotonic() - 61]) for i in range(10000)})
    assert not main.limited(windows, "new", 5) and len(windows) == 1
    windows.clear()
    for i in range(10000):
        windows[str(i)] = deque([time.monotonic()])
    assert main.limited(windows, "overflow", 5)
    assert len(windows) == 10000


def test_secrets_repr_and_json_logging():
    values = {key: "private-" + key for key in ("session_secret", "admin_password", "admin_password_hash", "twitch_client_secret", "youtube_api_key")}
    config = Settings(_env_file=None, **values)
    assert all(value not in repr(config) for value in values.values())
    record = logging.makeLogRecord({"name": "soe.access", "levelname": "INFO", "msg": "request", "request_id": "abc", "method": "GET", "path": "/health", "status": 200, "duration_ms": 1})
    entry = json.loads(JsonFormatter().format(record))
    assert entry["request_id"] == "abc" and entry["status"] == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("env", ["production", "staging"])
async def test_no_bypass_outside_development(client, monkeypatch, env):
    monkeypatch.setattr(auth.settings, "env", env)
    monkeypatch.setattr(auth.settings, "demo_mode", True)
    monkeypatch.setattr(auth.settings, "admin_password", SecretStr(""))
    monkeypatch.setattr(auth.settings, "admin_password_hash", SecretStr(""))
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    assert (await client.post("/api/v1/auth/login", data={"username": "operator", "password": "anything"})).status_code == 503


@pytest.mark.asyncio
async def test_processing_rechecks_path_and_hides_errors(client, isolated_database, tmp_path, monkeypatch):
    headers = await sign_in(client)
    with isolated_database() as db:
        source = Source(platform="upload", external_id="process-security", title="test", url="https://example.com/video", media_path="/etc/passwd")
        db.add(source)
        db.flush()
        source_id = source.id
        db.add(RightsRecord(source_id=source_id, authorization_type="owned", commercial_use=True, publishing_allowed=True, allowed_platforms=['youtube'], evidence="Owned test media"))
        db.commit()
    assert (await client.post(f"/api/v1/sources/{source_id}/process", headers=headers)).status_code == 422
    media = tmp_path / "media" / "sample.mp4"
    media.parent.mkdir()
    media.write_bytes(b"test")
    with isolated_database() as db:
        db.get(Source, source_id).media_path = str(media)
        db.commit()

    response = await client.post(f"/api/v1/sources/{source_id}/process", headers=headers)
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    with isolated_database() as db:
        assert db.query(main.WorkflowRun).one().state == "queued"


@pytest.mark.asyncio
async def test_access_log_no_query_and_rate_limit_headers(client, monkeypatch, caplog):
    monkeypatch.setattr(auth.settings, "rate_limit_per_minute", 1)
    with caplog.at_level(logging.INFO, logger="soe.access"):
        response = await client.get("/health?token=private-query", headers={"X-Request-ID": "request-log-test"})
    assert response.status_code == 200
    record = next(record for record in caplog.records if record.name == "soe.access")
    assert record.request_id == "request-log-test" and record.path == "/health" and record.status == 200
    assert "private-query" not in JsonFormatter().format(record)
    response = await client.get("/health", headers={"X-Request-ID": "invalid request id"})
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert re.fullmatch(r"[a-f0-9]{32}", response.headers["X-Request-ID"])
    assert "Strict-Transport-Security" in response.headers


@pytest.mark.asyncio
async def test_login_validation_does_not_echo_password(client):
    response = await client.post("/api/v1/auth/login", data={"username": "operator", "password": "private-long-password" * 100})
    assert response.status_code == 422
    assert "private-long-password" not in response.text


@pytest.mark.asyncio
async def test_cors_explicit_allowlist(client):
    accepted = await client.options("/api/v1/sources", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "X-CSRF-Token,Content-Type"})
    assert accepted.status_code == 200
    assert accepted.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
    assert accepted.headers["Access-Control-Allow-Credentials"] == "true"
    rejected = await client.options("/api/v1/sources", headers={"Origin": "https://untrusted.example", "Access-Control-Request-Method": "POST"})
    assert rejected.status_code == 400 and "Access-Control-Allow-Origin" not in rejected.headers
