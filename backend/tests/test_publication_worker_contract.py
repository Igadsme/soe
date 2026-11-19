"""Worker and provider contracts through the normal queue and a mock transport."""
import json
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import publishing, worker
from app.config import settings
from app.main import app
from app.models import Clip, ConnectedAccount, Job, Publication, RightsRecord, Source, now
from app.publication_worker import PublisherJob
from app.publishers import instagram, tiktok
from app.token_vault import token_vault

ACCESS = "fixture-access"
REFRESH = "fixture-refresh"
NEW_ACCESS = "fixture-new-access"
CHUNK = 256 * 1024
YT_INIT = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet%2Cstatus"
YT_SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=fixture"
TIKTOK = "https://open.tiktokapis.com/v2/post/publish"
TIKTOK_UPLOAD = "https://upload.us.tiktokapis.com/video/?upload_id=fixture&upload_token=fixture"
GRAPH = "https://graph.facebook.com/v26.0"


@pytest.fixture
def scenario(isolated_database, monkeypatch):
    monkeypatch.setattr(settings, "token_encryption_keys", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "publishing_enabled", True)
    monkeypatch.setattr(settings, "publishing_dry_run", False)
    monkeypatch.setattr(settings, "publishing_max_attempts", 3)
    monkeypatch.setattr(settings, "tiktok_app_audited", False)
    monkeypatch.setattr(settings, "youtube_upload_chunk_bytes", CHUNK)
    monkeypatch.setattr(settings, "google_client_id", "google-client-fixture")
    monkeypatch.setattr(settings, "google_client_secret", "google-secret-fixture")
    monkeypatch.setattr(settings, "tiktok_client_key", "tiktok-client-fixture")
    monkeypatch.setattr(settings, "tiktok_client_secret", "tiktok-secret-fixture")
    monkeypatch.setattr(settings, "meta_graph_version", "v26.0")
    monkeypatch.setattr(tiktok, "POLL_DELAYS", (0, 0))
    monkeypatch.setattr(instagram, "POLL_DELAYS", (0, 0))

    def create(platform="youtube", media=b"rendered fixture bytes", duration=10, **payload):
        monkeypatch.setattr(settings, f"publish_{platform}_enabled", True)
        path = Path(settings.media_root) / "render.mp4"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(media)
        with isolated_database() as db:
            source = Source(platform="upload", external_id="source-fixture", title="Source", url="https://example.test/source")
            db.add(source)
            db.flush()
            clip = Clip(source_id=source.id, start_seconds=0, end_seconds=duration, description="Description",
                        hook="Hook", score=10, status="rendered", output_path=str(path))
            rights = RightsRecord(source_id=source.id, authorization_type="owned", evidence="Rights fixture",
                                  allowed_platforms=[platform], publishing_allowed=True, commercial_use=True)
            vault = token_vault()
            account = ConnectedAccount(platform=platform, external_account_id="123", display_name="Account",
                                       access_token=vault.encrypt(ACCESS.encode()).decode(),
                                       refresh_token=vault.encrypt(REFRESH.encode()).decode())
            db.add_all([clip, account, rights])
            db.commit()
        body = {"platform": platform, "account_id": account.id, **payload}
        with TestClient(app) as client:
            response = client.post(f"/api/v1/clips/{clip.id}/publish", json=body)
        return response, clip, account, path

    return create


def transport(monkeypatch, handler):
    async def check(request):
        await request.aread()
        return handler(request)
    monkeypatch.setattr(publishing, "provider_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(check)))


def publication(db_factory):
    with db_factory() as db:
        return db.scalar(select(Publication))


def allow_retry(db_factory):
    with db_factory() as db:
        db.scalar(select(Job)).run_after = now() - timedelta(seconds=1)
        db.commit()


@pytest.mark.asyncio
async def test_youtube_resumable_upload_contract(scenario, isolated_database, monkeypatch):
    video = bytes(range(256)) * 1101
    queued, _, _, _ = scenario(media=video, privacy_status="unlisted")
    assert queued.status_code == 200
    recorded = []

    def handle(request):
        assert request.headers["authorization"] == f"Bearer {ACCESS}"
        recorded.append(request)
        index = len(recorded)
        if index == 1:
            assert request.method == "POST" and str(request.url) == YT_INIT
            assert request.headers["x-upload-content-type"] == "video/mp4"
            assert request.headers["x-upload-content-length"] == str(len(video))
            assert request.headers["content-type"] == "application/json"
            assert json.loads(request.content) == {
                "snippet": {"title": "Hook", "description": "Description", "categoryId": "22"},
                "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False},
            }
            return httpx.Response(200, headers={"Location": YT_SESSION})
        assert request.method == "PUT" and str(request.url) == YT_SESSION
        assert request.headers["content-type"] == "video/mp4"
        assert YT_SESSION not in publication(isolated_database).upload_checkpoint
        if index == 2:
            assert request.headers["content-range"] == f"bytes 0-{CHUNK - 1}/{len(video)}"
            return httpx.Response(308, headers={"Range": f"bytes=0-{CHUNK - 1}"})
        assert request.headers["content-range"] == f"bytes {CHUNK}-{len(video) - 1}/{len(video)}"
        assert publication(isolated_database).bytes_sent == CHUNK
        return httpx.Response(201, json={"id": "example-video"})

    transport(monkeypatch, handle)
    assert await worker.process_once() == 1
    assert b"".join(request.content for request in recorded[1:]) == video
    row = publication(isolated_database)
    assert row.status == "succeeded" and row.bytes_sent == row.bytes_total == len(video)
    assert row.external_id == "example-video" and row.published_at
    assert row.external_url == "https://www.youtube.com/watch?v=example-video"
    assert row.upload_checkpoint is None
    with TestClient(app) as client:
        data, = client.get("/api/v1/publications").json()
        assert data["upload_progress"] == {"bytes_sent": len(video), "bytes_total": len(video)}
        assert "upload_checkpoint" not in data
        states = client.get(f"/api/v1/publications/{row.id}/events").json()
        assert [(item["from_status"], item["to_status"]) for item in states] == [
            (None, "queued"), ("queued", "running"), ("running", "succeeded"),
        ]


@pytest.mark.parametrize("accepted", [True, False])
@pytest.mark.asyncio
async def test_youtube_worker_crash_recovery_contract(accepted, scenario, isolated_database, monkeypatch):
    video = b"media" * 5
    assert scenario(media=video)[0].status_code == 200
    calls = []
    job_id = worker.claim_job("crashed-worker")

    class SimulatedCrash(BaseException):
        pass

    def handle(request):
        calls.append(request)
        assert request.headers["authorization"] == f"Bearer {ACCESS}"
        if request.method == "POST":
            assert str(request.url) == YT_INIT
            return httpx.Response(200, headers={"Location": YT_SESSION})
        assert request.method == "PUT" and str(request.url) == YT_SESSION
        if request.headers["content-range"] == f"bytes */{len(video)}":
            assert not request.content and request.headers["content-length"] == "0"
            if accepted:
                return httpx.Response(200, json={"id": "retained"})
            return httpx.Response(308, headers={"Range": "bytes=0-4"})
        if len(calls) == 2:
            assert request.content == video
            raise SimulatedCrash
        assert request.headers["content-range"] == f"bytes 5-{len(video) - 1}/{len(video)}"
        assert request.content == video[5:]
        return httpx.Response(200, json={"id": "retained"})

    transport(monkeypatch, handle)
    publisher = PublisherJob(isolated_database, lambda: worker.owned(job_id, "crashed-worker"))
    with pytest.raises(SimulatedCrash):
        await publisher.execute()
    with isolated_database() as db:
        job = db.get(Job, job_id)
        job.lease_expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert await worker.process_once() == 1
    assert len([request for request in calls if request.method == "POST"]) == 1
    assert len([request for request in calls if request.method == "PUT" and request.content]) == (1 if accepted else 2)
    row = publication(isolated_database)
    assert row.status == "succeeded" and row.attempts == 2 and row.external_id == "retained"


@pytest.mark.parametrize(("reason", "error", "state"), [
    ("quotaExceeded", "rate_limited", "queued"), ("uploadLimitExceeded", "rate_limited", "queued"),
    ("insufficientPermissions", "permission", "failed"), ("invalidTitle", "invalid", "failed"),
])
@pytest.mark.asyncio
async def test_youtube_error_classification_contract(reason, error, state, scenario, isolated_database, monkeypatch):
    assert scenario()[0].status_code == 200

    def handle(request):
        assert request.method == "POST" and str(request.url) == YT_INIT
        status = 400 if error == "invalid" else 403
        return httpx.Response(status, headers={"Retry-After": "111"}, json={"error": {
            "message": "private fixture provider message", "errors": [{"reason": reason}],
        }})

    transport(monkeypatch, handle)
    assert await worker.process_once() == 1
    row = publication(isolated_database)
    assert (row.status, row.error_class) == (state, error)
    assert "private fixture" not in row.last_error
    if state == "queued":
        assert publishing.utc(row.next_attempt_at) > now() + timedelta(seconds=100)
    else:
        with TestClient(app) as client:
            assert client.post(f"/api/v1/publications/{row.id}/retry").status_code == 409


@pytest.mark.parametrize("revoked", [True, False])
@pytest.mark.asyncio
async def test_youtube_401_refresh_then_retry_contract(revoked, scenario, isolated_database, monkeypatch):
    response, _, account, _ = scenario()
    assert response.status_code == 200
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            assert request.method == "POST" and str(request.url) == YT_INIT
            assert request.headers["authorization"] == f"Bearer {ACCESS}"
            return httpx.Response(401)
        if len(calls) == 2:
            assert request.method == "POST" and str(request.url) == "https://oauth2.googleapis.com/token"
            assert parse_qs(request.content.decode()) == {
                "client_id": ["google-client-fixture"], "client_secret": ["google-secret-fixture"],
                "grant_type": ["refresh_token"], "refresh_token": [REFRESH],
            }
            if revoked:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": NEW_ACCESS, "expires_in": 3600})
        assert request.headers["authorization"] == f"Bearer {NEW_ACCESS}"
        if len(calls) == 3:
            assert str(request.url) == YT_INIT and request.content == calls[0].content
            return httpx.Response(200, headers={"Location": YT_SESSION})
        assert str(request.url) == YT_SESSION
        return httpx.Response(200, json={"id": "published"})

    transport(monkeypatch, handle)
    assert await worker.process_once() == 1
    row = publication(isolated_database)
    with isolated_database() as db:
        current = db.get(ConnectedAccount, account.id)
        if revoked:
            assert len(calls) == 2 and row.error_class == "auth_revoked" and row.status == "failed"
            assert current.status == "revoked" and current.access_token is None and current.refresh_token is None
        else:
            assert len(calls) == 4 and row.status == "succeeded"
            assert token_vault().decrypt(current.access_token.encode()).decode() == NEW_ACCESS
            assert token_vault().decrypt(current.refresh_token.encode()).decode() == REFRESH


@pytest.mark.parametrize(("total", "size", "count"), [
    (3, 3, 1), (5_000_000, 5_000_000, 1), (64_000_000, 64_000_000, 1),
    (64_000_001, 16_000_000, 4), (90_000_011, 16_000_000, 5),
    (4_000_000_000, 16_000_000, 250),
])
def test_tiktok_chunk_math(total, size, count):
    assert tiktok.chunk_plan(total) == (size, count)
    final_size = total - (count - 1) * size
    assert size <= final_size <= 128_000_000


@pytest.mark.parametrize("result", ["success", "failed", "privacy", "duration"])
@pytest.mark.asyncio
async def test_tiktok_direct_post_contract(result, scenario, isolated_database, monkeypatch):
    content = b"rendered TikTok file"
    assert scenario(platform="tiktok", media=content)[0].status_code == 200
    calls = []

    def handle(request):
        calls.append(request)
        i = len(calls)
        if i == 3:
            assert request.method == "PUT" and str(request.url) == TIKTOK_UPLOAD
            assert "authorization" not in request.headers
            assert request.headers["content-type"] == "video/mp4"
            assert request.headers["content-range"] == f"bytes 0-{len(content) - 1}/{len(content)}"
            assert request.headers["content-length"] == str(len(content)) and request.content == content
            return httpx.Response(201)
        assert request.method == "POST" and request.headers["authorization"] == f"Bearer {ACCESS}"
        if i == 1:
            assert str(request.url) == f"{TIKTOK}/creator_info/query/" and not request.content
            data = {
                "privacy_level_options": ["SELF_ONLY"] if result != "privacy" else ["PUBLIC_TO_EVERYONE"],
                "max_video_post_duration_sec": 60 if result != "duration" else 5,
                "comment_disabled": True, "duet_disabled": False, "stitch_disabled": True,
            }
        elif i == 2:
            assert str(request.url) == f"{TIKTOK}/video/init/"
            assert json.loads(request.content) == {
                "post_info": {"title": "Hook", "privacy_level": "SELF_ONLY", "disable_comment": True,
                              "disable_duet": False, "disable_stitch": True},
                "source_info": {"source": "FILE_UPLOAD", "video_size": len(content),
                                "chunk_size": len(content), "total_chunk_count": 1},
            }
            data = {"publish_id": "video.fixture", "upload_url": TIKTOK_UPLOAD}
        else:
            assert str(request.url) == f"{TIKTOK}/status/fetch/"
            assert json.loads(request.content) == {"publish_id": "video.fixture"}
            data = {"status": "PROCESSING_UPLOAD" if i == 4 else "FAILED" if result == "failed" else "PUBLISH_COMPLETE"}
        return httpx.Response(200, json={"data": data, "error": {"code": "ok"}})

    transport(monkeypatch, handle)
    assert await worker.process_once() == 1
    row = publication(isolated_database)
    if result == "success":
        assert len(calls) == 5 and row.status == "succeeded" and row.external_id == "video.fixture"
    else:
        assert len(calls) == (5 if result == "failed" else 1)
        assert row.status == "failed" and row.error_class == ("permission" if result == "privacy" else "invalid")


def test_unaudited_tiktok_visibility_is_rejected(scenario):
    response, *_ = scenario(platform="tiktok", tiktok_privacy_level="PUBLIC_TO_EVERYONE")
    assert response.status_code == 422 and "audited" in response.json()["detail"]


@pytest.mark.parametrize("result", ["success", "quota", "expired", "lost_publish_response"])
@pytest.mark.asyncio
async def test_instagram_resumable_reel_contract(result, scenario, isolated_database, monkeypatch):
    content = b"rendered reel bytes"
    assert scenario(platform="instagram", media=content, instagram_visibility_acknowledged=True)[0].status_code == 200
    calls = []

    def handle(request):
        calls.append(request)
        assert "access_token=" not in str(request.url) and ACCESS not in str(request.url)
        i = len(calls)
        if i == 3:
            assert request.method == "POST" and str(request.url) == "https://rupload.facebook.com/ig-api-upload/v26.0/container"
            assert request.headers["authorization"] == f"OAuth {ACCESS}"
            assert request.headers["offset"] == "0"
            assert request.headers["file_size"] == request.headers["content-length"] == str(len(content))
            assert request.content == content
            return httpx.Response(200, json={"success": True})
        assert request.headers["authorization"] == f"Bearer {ACCESS}"
        if i == 1:
            assert request.method == "GET" and str(request.url) == f"{GRAPH}/123/content_publishing_limit?fields=quota_usage%2Cconfig"
            return httpx.Response(200, json={"data": [{"quota_usage": 100 if result == "quota" else 1, "config": {"quota_total": 100}}]})
        if i == 2:
            assert request.method == "POST" and str(request.url) == f"{GRAPH}/123/media"
            assert parse_qs(request.content.decode()) == {
                "media_type": ["REELS"], "upload_type": ["resumable"], "caption": ["Description"],
            }
            return httpx.Response(200, json={"id": "container"})
        if i == 6:
            assert request.method == "POST" and str(request.url) == f"{GRAPH}/123/media_publish"
            assert parse_qs(request.content.decode()) == {"creation_id": ["container"]}
            if result == "lost_publish_response":
                raise httpx.ReadTimeout("fixture")
            return httpx.Response(200, json={"id": "reel-id"})
        assert request.method == "GET" and str(request.url) == f"{GRAPH}/container?fields=status_code%2Cstatus"
        status = "EXPIRED" if result == "expired" else {4: "IN_PROGRESS", 5: "FINISHED", 7: "PUBLISHED"}[i]
        return httpx.Response(200, json={"status_code": status})

    transport(monkeypatch, handle)
    assert await worker.process_once() == 1
    if result == "lost_publish_response":
        assert publication(isolated_database).error_class == "transient"
        allow_retry(isolated_database)
        assert await worker.process_once() == 1
    row = publication(isolated_database)
    if result in {"success", "lost_publish_response"}:
        assert row.status == "succeeded" and row.published_at
        assert len([request for request in calls if request.url.path.endswith("/media_publish")]) == 1
        assert row.external_id == ("reel-id" if result == "success" else None)
    else:
        assert row.error_class == ("rate_limited" if result == "quota" else "invalid")


@pytest.mark.parametrize(("condition", "status", "error"), [
    ("rights", "blocked", "rights"), ("global", "failed", "disabled"), ("platform", "failed", "disabled"),
    ("quota", "queued", "rate_limited"), ("dry_run", "dry_run", None), ("revoked", "failed", "auth_revoked"),
    ("changed_media", "failed", "invalid"), ("missing_media", "failed", "invalid"),
])
@pytest.mark.asyncio
async def test_worker_policy_without_provider_calls(condition, status, error, scenario, isolated_database, monkeypatch):
    if condition == "dry_run":
        monkeypatch.setattr(settings, "publishing_dry_run", True)
    queued, clip, account, path = scenario()
    assert queued.status_code == 200
    with isolated_database() as db:
        if condition == "rights":
            db.scalar(select(RightsRecord)).revoked = True
        elif condition == "global":
            monkeypatch.setattr(settings, "publishing_enabled", False)
        elif condition == "platform":
            monkeypatch.setattr(settings, "publish_youtube_enabled", False)
        elif condition == "quota":
            db.get(ConnectedAccount, account.id).daily_upload_limit = 1
            db.add(Publication(account_id=account.id, clip_id=clip.id, platform="youtube", status="succeeded", published_at=now()))
        elif condition == "dry_run":
            monkeypatch.setattr(settings, "publishing_dry_run", False)
        elif condition == "revoked":
            db.get(ConnectedAccount, account.id).status = "revoked"
        elif condition == "changed_media":
            path.write_bytes(b"x" * path.stat().st_size)
        elif condition == "missing_media":
            path.unlink()
        db.commit()
    transport(monkeypatch, lambda _: pytest.fail("Provider request bypassed publication policy"))
    assert await worker.process_once() == 1
    with isolated_database() as db:
        row = db.get(Publication, queued.json()["publication_id"])
        assert row.status == status and row.error_class == error
        assert row.published_at is None and row.bytes_sent == 0


@pytest.mark.asyncio
async def test_scheduled_publication_waits_and_cancels(scenario, monkeypatch):
    queued, *_ = scenario(scheduled_at=(now() + timedelta(days=1)).isoformat())
    assert queued.status_code == 200 and queued.json()["status"] == "scheduled"
    identifier = queued.json()["publication_id"]
    transport(monkeypatch, lambda _: pytest.fail("Scheduled upload executed early"))
    assert await worker.process_once() == 0
    with TestClient(app) as client:
        assert client.post(f"/api/v1/publications/{identifier}/cancel").json()["status"] == "cancelled"
        assert client.post(f"/api/v1/publications/{identifier}/cancel").status_code == 409
        assert client.get(f"/api/v1/publications/{identifier}/events").json()[-1]["to_status"] == "cancelled"
    assert await worker.process_once() == 0


@pytest.mark.asyncio
async def test_transient_errors_dead_letter_and_can_be_retried(scenario, isolated_database, monkeypatch):
    queued, *_ = scenario()
    identifier = queued.json()["publication_id"]
    transport(monkeypatch, lambda _: httpx.Response(503, json={"error": {"message": "fixture secret"}}))
    for _ in range(3):
        assert await worker.process_once() == 1
        allow_retry(isolated_database)
    assert await worker.process_once() == 0
    row = publication(isolated_database)
    assert row.status == "failed" and row.attempts == 3 and row.error_class == "transient"
    with TestClient(app) as client:
        assert client.post(f"/api/v1/publications/{identifier}/retry").json()["status"] == "queued"
        assert client.post(f"/api/v1/publications/{identifier}/retry").status_code == 409
    assert publication(isolated_database).attempts == 0


@pytest.mark.asyncio
async def test_tiktok_refresh_rotation_and_resume_contract(scenario, isolated_database, monkeypatch):
    media = b"fixture TikTok video"
    response, _, account, _ = scenario(platform="tiktok", media=media)
    assert response.status_code == 200
    with isolated_database() as db:
        db.get(ConnectedAccount, account.id).expires_at = now() + timedelta(seconds=30)
        db.commit()
    requests = []
    def handle(request):
        requests.append(request)
        calls = len(requests)
        if calls == 1:
            assert request.method == "POST" and str(request.url) == "https://open.tiktokapis.com/v2/oauth/token/"
            assert request.headers["cache-control"] == "no-cache"
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            assert parse_qs(request.content.decode()) == {
                "client_key": ["tiktok-client-fixture"], "client_secret": ["tiktok-secret-fixture"],
                "grant_type": ["refresh_token"], "refresh_token": [REFRESH],
            }
            return httpx.Response(200, json={"access_token": NEW_ACCESS, "refresh_token": "rotated-fixture", "expires_in": 3600})
        if calls == 4:
            assert request.method == "PUT" and str(request.url) == TIKTOK_UPLOAD and request.content == media
            raise httpx.ReadTimeout("The test provider accepted the media but its response was lost.")
        assert request.method == "POST" and request.headers["authorization"] == f"Bearer {NEW_ACCESS}"
        assert request.headers["content-type"] == "application/json; charset=UTF-8"
        if calls == 2:
            assert str(request.url) == f"{TIKTOK}/creator_info/query/"
            data = {"privacy_level_options": ["SELF_ONLY"], "max_video_post_duration_sec": 60}
        elif calls == 3:
            assert str(request.url) == f"{TIKTOK}/video/init/"
            data = {"publish_id": "video.fixture", "upload_url": TIKTOK_UPLOAD}
        else:
            assert str(request.url) == f"{TIKTOK}/status/fetch/"
            assert json.loads(request.content) == {"publish_id": "video.fixture"}
            data = {"status": "PROCESSING_UPLOAD", "uploaded_bytes": len(media)} if calls == 5 else {"status": "PUBLISH_COMPLETE"}
        return httpx.Response(200, json={"data": data, "error": {"code": "ok"}})
    transport(monkeypatch, handle)
    assert await worker.process_once() == 1
    assert publication(isolated_database).error_class == "transient"
    allow_retry(isolated_database)
    assert await worker.process_once() == 1
    assert publication(isolated_database).status == "succeeded"
    assert sum(request.method == "PUT" for request in requests) == 1
    with isolated_database() as db:
        current = db.get(ConnectedAccount, account.id)
        assert token_vault().decrypt(current.refresh_token.encode()).decode() == "rotated-fixture"


def test_ig_visibility_acknowledgement(scenario):
    response, *_ = scenario(platform="instagram")
    assert response.status_code == 422 and "account" in response.json()["detail"]


@pytest.mark.asyncio
async def test_revoke_rights_after_youtube_initialization_contract(scenario, isolated_database, monkeypatch):
    assert scenario()[0].status_code == 200
    requests = []
    def handle(request):
        requests.append(request)
        assert request.method == "POST" and str(request.url) == YT_INIT
        with isolated_database() as db:
            db.scalar(select(RightsRecord)).revoked = True
            db.commit()
        return httpx.Response(200, headers={"Location": YT_SESSION})
    transport(monkeypatch, handle)
    assert await worker.process_once() == 1
    assert len(requests) == 1
    row = publication(isolated_database)
    assert row.status == "blocked" and row.bytes_sent == 0


def test_queue_validates_platform_account_and_existing_media(scenario, isolated_database):
    response, clip, account, path = scenario()
    assert response.status_code == 200
    body = {"platform": "youtube", "account_id": account.id}
    with isolated_database() as db:
        db.get(ConnectedAccount, account.id).platform = "tiktok"
        db.commit()
    with TestClient(app) as client:
        assert client.post(f"/api/v1/clips/{clip.id}/publish", json=body).status_code == 409
    with isolated_database() as db:
        db.get(ConnectedAccount, account.id).platform = "youtube"
        db.commit()
    path.unlink()
    with TestClient(app) as client:
        assert client.post(f"/api/v1/clips/{clip.id}/publish", json=body).status_code == 422
