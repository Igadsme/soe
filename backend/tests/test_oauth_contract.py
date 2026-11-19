"""OAuth contracts using only httpx.MockTransport."""
import base64
import logging
from datetime import timedelta
from hashlib import sha256
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet, InvalidToken
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import auth, oauth, publishing
from app.config import settings
from app.logging_config import RedactCredentials, redact
from app.main import app
from app.models import ConnectedAccount, OAuthState, now
from app.token_vault import keyring, rotate_tokens

ORIGIN = "https://soe.example.test"
META = "https://graph.facebook.com/v26.0"
CODE = "fixture-code"
ACCESS = "fixture-oauth-access"
REFRESH = "fixture-oauth-refresh"


@pytest.fixture
def oauth_config(monkeypatch):
    for name, value in {
        "dev_auth_bypass": False, "session_secret": "test-only-session" * 4,
        "oauth_redirect_base": ORIGIN, "token_encryption_keys": Fernet.generate_key().decode(),
        "google_client_id": "google-fixture", "google_client_secret": "google-secret-fixture",
        "tiktok_client_key": "tiktok-fixture", "tiktok_client_secret": "tiktok-secret-fixture",
        "meta_app_id": "meta-fixture", "meta_app_secret": "meta-secret-fixture",
        "meta_graph_version": "v26.0",
    }.items():
        monkeypatch.setattr(settings, name, value)


def form(request):
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    return dict(request.url.params) | {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


def login(client):
    cookie = auth.make_session("operator")
    client.cookies.set("soe_session", cookie)
    client.cookies.set("soe_csrf", auth.csrf_token(cookie))
    return cookie


def mock_provider(monkeypatch, handler):
    monkeypatch.setattr(publishing, "provider_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("platform", ["youtube", "tiktok", "instagram"])
def test_oauth_connect_and_revoke_contract(platform, oauth_config, isolated_database, monkeypatch):
    calls = []
    pkce = {}
    callback = f"{ORIGIN}/api/v1/oauth/{platform}/callback"

    def handler(request):
        calls.append((request.method, str(request.url)))
        url = str(request.url)
        assert "access_token=" not in url and ACCESS not in url and REFRESH not in url
        if url == oauth.GOOGLE_EXCHANGE_URL:
            assert request.method == "POST" and not request.url.query
            assert form(request) == {
                "client_id": "google-fixture", "client_secret": "google-secret-fixture",
                "code": CODE, "redirect_uri": callback, "grant_type": "authorization_code",
                "code_verifier": pkce["verifier"],
            }
            return httpx.Response(200, json={"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600})
        if request.url.path == "/youtube/v3/channels":
            assert url == "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true"
            assert request.method == "GET" and request.headers["authorization"] == f"Bearer {ACCESS}"
            return httpx.Response(200, json={"items": [{"id": "123", "snippet": {"title": "Demo channel"}}]})
        if url == oauth.TIKTOK_EXCHANGE_URL:
            assert request.method == "POST" and not request.url.query
            assert request.headers["cache-control"] == "no-cache"
            assert form(request) == {
                "client_key": "tiktok-fixture", "client_secret": "tiktok-secret-fixture",
                "code": CODE, "redirect_uri": callback, "grant_type": "authorization_code",
            }
            return httpx.Response(200, json={"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600,
                                             "scope": "video.publish,user.info.basic"})
        if request.url.path == "/v2/user/info/":
            assert url == "https://open.tiktokapis.com/v2/user/info/?fields=open_id%2Cdisplay_name"
            assert request.method == "GET" and request.headers["authorization"] == f"Bearer {ACCESS}"
            return httpx.Response(200, json={"data": {"user": {"open_id": "123", "display_name": "Demo channel"}}, "error": {"code": "ok"}})
        if url == f"{META}/oauth/access_token":
            assert request.method == "POST" and not request.url.query
            if len(calls) == 1:
                assert form(request) == {"client_id": "meta-fixture", "client_secret": "meta-secret-fixture",
                                         "code": CODE, "redirect_uri": callback}
                return httpx.Response(200, json={"access_token": ACCESS})
            assert form(request) == {"client_id": "meta-fixture", "client_secret": "meta-secret-fixture",
                                     "grant_type": "fb_exchange_token", "fb_exchange_token": ACCESS}
            return httpx.Response(200, json={"access_token": REFRESH, "expires_in": 60 * 86400})
        if request.url.path == "/v26.0/me/accounts":
            assert request.method == "GET" and request.headers["authorization"] == f"Bearer {REFRESH}"
            assert dict(request.url.params) == {"fields": "id,access_token,instagram_business_account{id,username}"}
            return httpx.Response(200, json={"data": [{"id": "page", "access_token": ACCESS,
                                                      "instagram_business_account": {"id": "123", "username": "Demo channel"}}]})
        if url == "https://oauth2.googleapis.com/revoke":
            assert request.method == "POST" and form(request) == {"token": REFRESH}
            return httpx.Response(200)
        if url == "https://open.tiktokapis.com/v2/oauth/revoke/":
            assert request.method == "POST" and form(request) == {
                "client_key": "tiktok-fixture", "client_secret": "tiktok-secret-fixture", "token": ACCESS,
            }
            return httpx.Response(200)
        assert url == f"{META}/me/permissions" and request.method == "DELETE"
        assert request.headers["authorization"] == f"Bearer {REFRESH}"
        return httpx.Response(500)

    mock_provider(monkeypatch, handler)
    with TestClient(app) as client:
        assert client.get(f"/api/v1/oauth/{platform}/start").status_code == 401
        cookie = login(client)
        start = client.get(f"/api/v1/oauth/{platform}/start")
        assert start.status_code == 200 and start.headers["cache-control"] == "no-store"
        url = urlsplit(start.json()["authorization_url"])
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        state = query.pop("state")
        base = {"redirect_uri": callback, "response_type": "code"}
        if platform == "youtube":
            assert url.netloc + url.path == "accounts.google.com/o/oauth2/v2/auth"
            with isolated_database() as db:
                pkce["verifier"] = db.get(OAuthState, state).code_verifier
            digest = base64.urlsafe_b64encode(sha256(pkce["verifier"].encode()).digest()).decode().rstrip("=")
            assert query == {**base, "client_id": "google-fixture", "scope": " ".join(oauth.SCOPES["youtube"]),
                             "code_challenge": digest, "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"}
        elif platform == "tiktok":
            assert url.netloc + url.path == "www.tiktok.com/v2/auth/authorize/"
            assert query == {**base, "client_key": "tiktok-fixture", "scope": "video.publish,user.info.basic"}
        else:
            assert url.netloc + url.path == "www.facebook.com/v26.0/dialog/oauth"
            assert query == {**base, "client_id": "meta-fixture", "scope": ",".join(oauth.SCOPES["instagram"])}
        route = f"/api/v1/oauth/{platform}/callback"
        result = client.get(route, params={"state": state, "code": CODE}, follow_redirects=False)
        assert result.status_code == 303
        assert result.headers["location"] == f"{ORIGIN}/?view=settings&oauth=success&platform={platform}"
        assert result.headers["referrer-policy"] == "no-referrer"
        account, = client.get("/api/v1/accounts").json()
        assert account["display_name"] == "Demo channel" and account["external_account_id"] == "123"
        assert "access_token" not in account and "refresh_token" not in account
        with isolated_database() as db:
            raw = db.execute(text("SELECT access_token, refresh_token FROM connected_accounts")).one()
            assert ACCESS not in raw[0] and REFRESH not in raw[1]
            vault = keyring(settings.token_encryption_keys)
            assert vault.decrypt(raw[0].encode()).decode() == ACCESS
            assert vault.decrypt(raw[1].encode()).decode() == REFRESH
            assert db.get(OAuthState, state).consumed_at is not None
        replay = client.get(route, params={"state": state, "code": CODE}, follow_redirects=False)
        assert "oauth=failure" in replay.headers["location"]
        assert len(calls) == (3 if platform == "instagram" else 2)
        route = f'/api/v1/accounts/{account["id"]}'
        assert client.delete(route).status_code == 403
        assert client.delete(route, headers={"X-CSRF-Token": auth.csrf_token(cookie)}).status_code == 204
        assert client.get("/api/v1/accounts").json()[0]["status"] == "revoked"
        with isolated_database() as db:
            assert tuple(db.execute(text("SELECT access_token, refresh_token FROM connected_accounts")).one()) == (None, None)


@pytest.mark.parametrize("case", ["bad", "expired", "mismatch", "session", "denied", "missing_code"])
def test_oauth_callback_state_guards_contract(case, oauth_config, isolated_database, monkeypatch):
    mock_provider(monkeypatch, lambda _: pytest.fail("Invalid callback reached a provider"))
    with TestClient(app) as client:
        login(client)
        path = "/api/v1/oauth/youtube"
        state = parse_qs(urlsplit(client.get(f"{path}/start").json()["authorization_url"]).query)["state"][0]
        parameters = {"state": state, "code": CODE}
        if case == "bad":
            parameters["state"] = "not-the-same-state"
        elif case == "expired":
            with isolated_database() as db:
                db.get(OAuthState, state).expires_at = now() - timedelta(seconds=1)
                db.commit()
        elif case == "mismatch":
            path = "/api/v1/oauth/tiktok"
        elif case == "session":
            login(client)
        elif case == "denied":
            parameters["error"] = "access_denied"
        else:
            del parameters["code"]
        response = client.get(f"{path}/callback", params=parameters, follow_redirects=False)
        assert response.status_code == 303 and "oauth=failure" in response.headers["location"]
        with isolated_database() as db:
            assert db.scalar(select(ConnectedAccount)) is None
            assert (db.get(OAuthState, state).consumed_at is not None) == (case in {"denied", "missing_code"})


def test_oauth_start_missing_config(oauth_config, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "")
    with TestClient(app) as client:
        login(client)
        response = client.get("/api/v1/oauth/youtube/start")
        assert response.status_code == 409 and "not configured" in response.json()["detail"]


@pytest.mark.asyncio
async def test_instagram_ineligible_contract(oauth_config):
    def handler(request):
        assert request.headers["authorization"] == f"Bearer {ACCESS}"
        assert request.url.path == "/v26.0/me/accounts"
        assert "access_token=" not in str(request.url)
        return httpx.Response(200, json={"data": [{"id": "999", "access_token": "fixture-page"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(publishing.PublishingUnavailable, match="eligible Instagram") as error:
            await oauth.meta_account(client, oauth.Credentials(ACCESS))
        assert error.value.error_class == "permission"


def test_fernet_rotation_reencrypts_tokens(isolated_database):
    previous, primary = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    with isolated_database() as db:
        old = keyring(previous)
        db.add(ConnectedAccount(platform="youtube", external_account_id="123", display_name="Channel",
                                access_token=old.encrypt(ACCESS.encode()).decode(),
                                refresh_token=old.encrypt(REFRESH.encode()).decode()))
        db.commit()
        ciphertext = db.scalar(select(ConnectedAccount)).access_token
        assert keyring(f"{primary},{previous}").decrypt(ciphertext.encode()) == ACCESS.encode()
        assert rotate_tokens(db, keyring(f"{primary},{previous}")) == 2
        refreshed = db.scalar(select(ConnectedAccount))
        assert refreshed.access_token != ciphertext
        assert keyring(primary).decrypt(refreshed.refresh_token.encode()) == REFRESH.encode()
        with pytest.raises(InvalidToken):
            old.decrypt(refreshed.access_token.encode())


def test_redaction_masks_headers_fields_url_and_format_arguments():
    value = "https://api.example.test/?access_token=query-fake&keep=1 {'refresh_token': 'refresh-fake'} Authorization: Bearer header-fake bearer bare-fake Authorization: Basic basic-fake"
    for secret in ("query-fake", "refresh-fake", "header-fake", "bare-fake", "basic-fake"):
        assert secret not in redact(value)
    assert "keep=1" in redact(value)
    record = logging.LogRecord("test", logging.INFO, "", 1, "body=%s", ({"access_token": "argument-fake"},), None)
    record.path = "/callback?access_token=another-fake"
    assert RedactCredentials().filter(record)
    assert "argument-fake" not in record.getMessage() and "another-fake" not in record.path


@pytest.mark.asyncio
async def test_meta_long_lived_refresh_contract(oauth_config):
    encrypted = keyring(settings.token_encryption_keys).encrypt(REFRESH.encode()).decode()
    account = ConnectedAccount(platform="instagram", external_account_id="123", refresh_token=encrypted)
    calls = []
    def handler(request):
        calls.append(request)
        assert "access_token=" not in str(request.url) and REFRESH not in str(request.url)
        if request.method == "POST":
            assert str(request.url) == f"{META}/oauth/access_token"
            assert form(request) == {
                "client_id": "meta-fixture", "client_secret": "meta-secret-fixture",
                "grant_type": "fb_exchange_token", "fb_exchange_token": REFRESH,
            }
            return httpx.Response(200, json={"access_token": "renewed-user-fixture", "expires_in": 3600})
        assert request.method == "GET" and request.url.path == "/v26.0/me/accounts"
        assert request.headers["authorization"] == "Bearer renewed-user-fixture"
        return httpx.Response(200, json={"data": [
            {"instagram_business_account": {"id": "456"}, "access_token": "different-page-fixture"},
            {"instagram_business_account": {"id": "123"}, "access_token": "renewed-page-fixture"},
        ]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        renewed = await oauth.refresh(client, account)
    assert len(calls) == 2
    assert renewed.access == "renewed-page-fixture" and renewed.refresh == "renewed-user-fixture"
    assert renewed.expires_in == 3600
