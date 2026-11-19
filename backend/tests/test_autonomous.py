"""Autonomous orchestration: multi-cycle runs, fault injection, controls, and killable media children.

Media inference/rendering is stubbed in-process here (real media is covered by test_media_e2e and the
release-evidence runs); provider HTTP uses httpx.MockTransport contract fakes. Nothing reaches a real API.
"""
import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import main, media_tasks, publishing, scheduler, worker
from app.automation import PROCESSING_CATEGORY
from app.config import settings
from app.integrations import DiscoveredItem, ProviderNotConfigured
from app.media import MediaProbe, sha256_file
from app.models import (
    AuditEvent,
    Clip,
    ConnectedAccount,
    DiscoverySchedule,
    Job,
    LedgerEntry,
    MetricEvent,
    Publication,
    Source,
    now,
)
from app.token_vault import token_vault

RIGHTS = {"authorization_type": "owned", "commercial_use": True, "publishing_allowed": True,
          "allowed_platforms": ["youtube", "upload"], "evidence": "Operator-owned original test recording"}
YT_SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=autonomous"


class Faults:
    """Deterministic fault injection for the stubbed media stages."""

    def __init__(self):
        self.transcribe_failures = 0
        self.render_failures = 0
        self.transcribe_calls = 0
        self.render_calls = 0


@pytest.fixture
def pipeline(isolated_database, monkeypatch):
    faults = Faults()
    monkeypatch.setattr(worker.Path, "touch", lambda self: None)

    async def in_process(target, args, result_file):
        return await asyncio.to_thread(target, *args)

    def ingest(source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"ingested")
        return destination, f"digest-{Path(source).name}", MediaProbe(60, 1920, 1080, True)

    class Transcriber:
        def __init__(self, _model):
            pass

        def transcribe(self, _path):
            faults.transcribe_calls += 1
            if faults.transcribe_failures:
                faults.transcribe_failures -= 1
                raise RuntimeError("injected transcription failure")
            return ["segment"]

    def candidates(*_args, **_kwargs):
        return [SimpleNamespace(start=0, end=20, description="Original clip one", hook="Hook one", score=90, evidence={}),
                SimpleNamespace(start=30, end=50, description="Original clip two", hook="Hook two", score=40, evidence={})]

    def render(_source, output, *_args):
        faults.render_calls += 1
        if faults.render_failures:
            faults.render_failures -= 1
            raise RuntimeError("injected render failure")
        output.write_bytes(b"rendered-" + output.name.encode())

    monkeypatch.setattr(worker, "run_media_task", in_process)
    monkeypatch.setattr(media_tasks, "probe", lambda *a, **k: MediaProbe(60, 1920, 1080, True))
    monkeypatch.setattr(worker, "ingest_authorized", ingest)
    monkeypatch.setattr(worker, "FasterWhisperTranscriber", Transcriber)
    monkeypatch.setattr(worker, "extract_candidates", candidates)
    monkeypatch.setattr(worker, "render_vertical", render)
    monkeypatch.setattr(worker, "quality_check", lambda items, _audio: [
        {"clip_id": item["clip_id"], "reasons": [], "sha256": sha256_file(Path(item["output"]))} for item in items])
    return faults


def add_source(name, rights=True):
    path = Path(settings.media_root) / f"{name}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"original media " + name.encode())
    with TestClient(main.app) as client:
        response = client.post("/api/v1/sources", json={"platform": "upload", "external_id": name, "title": name,
                                                         "url": "https://example.test/" + name, "media_path": str(path)})
        assert response.status_code == 201, response.text
        source_id = response.json()["id"]
        if rights:
            assert client.post(f"/api/v1/sources/{source_id}/rights", json=RIGHTS).status_code == 201
    return source_id


def youtube_account(isolated_database, monkeypatch):
    monkeypatch.setattr(settings, "token_encryption_keys", Fernet.generate_key().decode())
    vault = token_vault()
    with isolated_database() as db:
        account = ConnectedAccount(platform="youtube", external_account_id="channel", display_name="Test channel",
                                   access_token=vault.encrypt(b"fixture-access").decode(),
                                   refresh_token=vault.encrypt(b"fixture-refresh").decode())
        db.add(account)
        db.commit()
        return account.id


class FakeYouTube:
    """Contract fake: resumable init + single PUT. Counts completed uploads per session."""

    def __init__(self, fail_first_puts=0):
        self.inits = 0
        self.completed = []
        self.fail_first_puts = fail_first_puts
        self.statistics = []

    def __call__(self, request):
        if request.method == "GET" and request.url.path == "/youtube/v3/videos":
            # Analytics collection (videos.list statistics) after publication.
            self.statistics.append(request.url.params["id"])
            return httpx.Response(200, json={"items": [{"id": request.url.params["id"], "statistics": {
                "viewCount": "12", "likeCount": "3", "commentCount": "1"}}]})
        if request.method == "POST":
            self.inits += 1
            return httpx.Response(200, headers={"Location": f"{YT_SESSION}{self.inits}"})
        if self.fail_first_puts:
            self.fail_first_puts -= 1
            return httpx.Response(503, json={"error": {"code": 503}})
        self.completed.append(str(request.url))
        return httpx.Response(201, json={"id": f"video-{len(self.completed)}"})


def install_transport(monkeypatch, fake):
    async def handler(request):
        await request.aread()
        return fake(request)
    monkeypatch.setattr(publishing, "provider_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def put_policy(**overrides):
    policy = {"auto_process_authorized": True, "approval_mode": "auto_after_qc", "min_clip_score": 70,
              "publishing_windows": [{"start_hour": 0, "end_hour": 24}], "timezone": "UTC",
              "max_concurrent_processing_jobs": 2, "max_concurrent_publish_jobs": 1, **overrides}
    with TestClient(main.app) as client:
        response = client.put("/api/v1/automation/policy", json=policy)
    assert response.status_code == 200, response.text
    return response.json()


async def drain(isolated_database, rounds=40):
    """One autonomous cycle: scheduler tick, then work until nothing is claimable."""
    worker.scheduler.scheduler_once(isolated_database)
    for _ in range(rounds):
        if not await worker.process_once():
            worker.scheduler.scheduler_once(isolated_database)
            if not await worker.process_once():
                return
    raise AssertionError("queue did not drain")


def make_due(isolated_database):
    with isolated_database() as db:
        for job in db.scalars(select(Job).where(Job.status == "queued")):
            job.run_after = now()
        db.commit()


@pytest.mark.asyncio
async def test_multi_cycle_autonomous_run_publishes_each_clip_once(pipeline, isolated_database, monkeypatch):
    account_id = youtube_account(isolated_database, monkeypatch)
    monkeypatch.setattr(settings, "publishing_enabled", True)
    monkeypatch.setattr(settings, "publish_youtube_enabled", True)
    monkeypatch.setattr(settings, "processing_cents_per_minute", 10)
    put_policy(auto_publish=True, target_accounts={"youtube": [account_id]}, daily_publish_limits={"youtube": 10},
               daily_budget_cents=100, monthly_budget_cents=1000)
    fake = FakeYouTube()
    install_transport(monkeypatch, fake)

    sources = [add_source(f"cycle-{index}") for index in range(3)]
    add_source("no-rights", rights=False)
    for _cycle in range(3):
        await drain(isolated_database)
        make_due(isolated_database)
    sources.append(add_source("late-arrival"))
    await drain(isolated_database)

    with isolated_database() as db:
        clips = list(db.scalars(select(Clip)))
        approved = [clip for clip in clips if clip.score >= 70]
        assert {clip.source_id for clip in clips} == set(sources)
        assert all(clip.status == ("approved" if clip.score >= 70 else "pending_review") for clip in clips)
        publications = list(db.scalars(select(Publication)))
        # Exactly one publication per approved clip/account, each uploaded exactly once.
        assert sorted(p.clip_id for p in publications) == sorted(c.id for c in approved)
        assert all(p.status == "succeeded" and p.privacy_status == "private" for p in publications), [
            (p.status, p.privacy_status, p.error_class, p.last_error) for p in publications]
        assert len(fake.completed) == len(publications) == fake.inits
        unauthorized = db.scalar(select(Source).where(Source.external_id == "no-rights"))
        assert unauthorized.status in {"discovered", "rights_review"}
        assert db.scalar(select(func.count()).select_from(Job).where(Job.source_id == unauthorized.id)) == 0
        spent = db.scalar(select(func.sum(LedgerEntry.cents)).where(LedgerEntry.category == PROCESSING_CATEGORY))
        assert spent == 4 * 10 and spent <= 100
        assert all(not entry.verified for entry in db.scalars(select(LedgerEntry)))
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "publish.attempt")) == len(publications)
        # Analytics were collected for published videos through the official statistics contract.
        assert set(fake.statistics) == {p.external_id for p in publications}
        assert db.scalar(select(func.count()).select_from(MetricEvent).where(MetricEvent.verified.is_(True))) > 0


@pytest.mark.asyncio
async def test_fault_injection_recovers_without_duplicates(pipeline, isolated_database, monkeypatch):
    account_id = youtube_account(isolated_database, monkeypatch)
    monkeypatch.setattr(settings, "publishing_enabled", True)
    monkeypatch.setattr(settings, "publish_youtube_enabled", True)
    put_policy(auto_publish=True, target_accounts={"youtube": [account_id]}, daily_publish_limits={"youtube": 5})
    fake = FakeYouTube(fail_first_puts=1)
    install_transport(monkeypatch, fake)
    pipeline.transcribe_failures = 1
    pipeline.render_failures = 1
    source_id = add_source("faulty")

    for _cycle in range(6):
        await drain(isolated_database)
        make_due(isolated_database)

    # Simulated worker crash: a claimed publish job whose lease expires is reclaimed, not duplicated.
    with isolated_database() as db:
        jobs = list(db.scalars(select(Job).where(Job.source_id == source_id, Job.kind == "process_source")))
        assert [job.status for job in jobs] == ["succeeded"] and jobs[0].attempts == 3
        publications = list(db.scalars(select(Publication)))
    assert pipeline.transcribe_calls == 3 and pipeline.render_calls >= 3
    assert len(publications) == 1 and publications[0].status == "succeeded", [
        (p.status, p.error_class, p.last_error, p.attempts) for p in publications]
    assert len(fake.completed) == 1


@pytest.mark.asyncio
async def test_worker_crash_mid_processing_is_reclaimed_once(pipeline, isolated_database):
    source_id = add_source("crash")
    put_policy()
    worker.scheduler.scheduler_once(isolated_database)
    job_id = worker.claim_job("crashed-worker")
    assert job_id
    with isolated_database() as db:
        job = db.get(Job, job_id)
        job.lease_expires_at = now()
        db.commit()
    await drain(isolated_database)
    with isolated_database() as db:
        job = db.get(Job, job_id)
        assert job.status == "succeeded" and job.attempts == 2 and job.locked_by is None
        assert db.scalar(select(func.count()).select_from(Job).where(Job.source_id == source_id,
                                                                     Job.kind == "process_source")) == 1


@pytest.mark.asyncio
async def test_dead_letter_and_replay(pipeline, isolated_database):
    pipeline.render_failures = 3
    source_id = add_source("dead")
    put_policy()
    for _ in range(4):
        await drain(isolated_database)
        make_due(isolated_database)
    with isolated_database() as db:
        job = db.scalar(select(Job).where(Job.source_id == source_id, Job.kind == "process_source"))
        assert job.status == "dead_letter" and job.error_class == "processing"
        assert "injected" not in (job.last_error or "")
        job_id = job.id
    with TestClient(main.app) as client:
        assert client.get("/api/v1/jobs", params={"status": "dead_letter"}).json()[0]["id"] == job_id
        replay = client.post(f"/api/v1/jobs/{job_id}/replay")
        assert replay.status_code == 200 and replay.json()["replay_of_id"] == job_id
        assert client.post(f"/api/v1/jobs/{job_id}/replay").json()["id"] == replay.json()["id"]
    await drain(isolated_database)
    with isolated_database() as db:
        assert db.get(Job, replay.json()["id"]).status == "succeeded"
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == "job.replay"))


def test_cancel_queued_and_running_jobs(pipeline, isolated_database):
    first = add_source("cancel-me")
    with TestClient(main.app) as client:
        job_id = client.post(f"/api/v1/sources/{first}/process").json()["job_id"]
        cancelled = client.post(f"/api/v1/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        assert client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 200
    second = add_source("cancel-running")
    with TestClient(main.app) as client:
        running_id = client.post(f"/api/v1/sources/{second}/process").json()["job_id"]
    assert worker.claim_job("runner") == running_id
    with TestClient(main.app) as client:
        assert client.post(f"/api/v1/jobs/{running_id}/cancel").json()["status"] == "cancel_requested"
    worker.finish_interruption(running_id, "runner")
    with isolated_database() as db:
        assert db.get(Job, running_id).status == "cancelled"


@pytest.mark.asyncio
async def test_emergency_stop_halts_scheduler_and_workers(pipeline, isolated_database):
    add_source("stopped")
    put_policy()
    with TestClient(main.app) as client:
        assert client.post("/api/v1/system/emergency-stop").json() == {"emergency_stopped": True}
        assert client.get("/api/v1/system/status").json()["emergency_stopped"] is True
    worker.scheduler.scheduler_once(isolated_database)
    assert await worker.process_once() == 0
    with isolated_database() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
    with TestClient(main.app) as client:
        assert client.post("/api/v1/system/resume").json() == {"emergency_stopped": False}
    await drain(isolated_database)
    with isolated_database() as db:
        assert db.scalar(select(Job.status).where(Job.kind == "process_source")) == "succeeded"
        actions = set(db.scalars(select(AuditEvent.action)))
        assert {"system.emergency_stop", "system.resume", "policy.update"} <= actions or {"system.emergency_stop", "system.resume"} <= actions


@pytest.mark.asyncio
async def test_emergency_stop_interrupts_running_job(pipeline, isolated_database):
    source_id = add_source("interrupt")
    with TestClient(main.app) as client:
        job_id = client.post(f"/api/v1/sources/{source_id}/process").json()["job_id"]
    assert worker.claim_job("runner") == job_id
    with TestClient(main.app) as client:
        client.post("/api/v1/system/emergency-stop")
    worker.finish_interruption(job_id, "runner")
    with isolated_database() as db:
        job = db.get(Job, job_id)
        assert job.status == "queued" and job.wait_reason == "emergency_stop"


@pytest.mark.asyncio
async def test_budget_ceiling_defers_processing(pipeline, isolated_database, monkeypatch):
    monkeypatch.setattr(settings, "processing_cents_per_minute", 100)
    source_id = add_source("expensive")
    put_policy(daily_budget_cents=50)
    with TestClient(main.app) as client:
        job_id = client.post(f"/api/v1/sources/{source_id}/process").json()["job_id"]
    assert await worker.process_once() == 1
    with isolated_database() as db:
        job = db.get(Job, job_id)
        assert job.status == "queued" and job.wait_reason == "budget_exhausted" and job.attempts == 0
        assert db.scalar(select(func.count()).select_from(LedgerEntry)) == 0
    assert pipeline.transcribe_calls == 0


@pytest.mark.asyncio
async def test_discovery_never_authorizes_public_content(pipeline, isolated_database, monkeypatch):
    class Adapter:
        async def discover(self, query, channels, limit):
            return [DiscoveredItem("youtube", "public-1", "Public cooking video", "https://www.youtube.com/watch?v=public-1",
                                   {"channelId": "UCallowed", "publishedAt": now().isoformat(), "duration": "PT2M"})]

    class Unconfigured:
        async def discover(self, *args):
            raise ProviderNotConfigured("not configured")

    monkeypatch.setitem(scheduler.DISCOVERY_ADAPTERS, "youtube", Adapter())
    monkeypatch.setitem(scheduler.DISCOVERY_ADAPTERS, "twitch", Unconfigured())
    put_policy()
    with TestClient(main.app) as client:
        for platform in ("youtube", "twitch"):
            response = client.post("/api/v1/discovery/schedules", json={
                "platform": platform, "query": "cooking pasta", "channel_allowlist": ["UCallowed"], "interval_minutes": 60})
            assert response.status_code == 201, response.text
    with isolated_database() as db:
        for row in db.scalars(select(DiscoverySchedule)):
            row.next_run_at = now()
        db.commit()
    await drain(isolated_database)
    with isolated_database() as db:
        source = db.scalar(select(Source).where(Source.external_id == "public-1"))
        assert source.status == "rights_review" and source.media_path is None
        assert source.metadata_json["discovery_rank"]["factors"]["channel_allowlist"] == 1
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == "process_source")) == 0
        statuses = {row.platform: row.last_status for row in db.scalars(select(DiscoverySchedule))}
    assert statuses == {"youtube": "succeeded", "twitch": "blocked: provider not configured"}


@pytest.mark.asyncio
async def test_rights_revoked_before_publish_blocks_upload(pipeline, isolated_database, monkeypatch):
    account_id = youtube_account(isolated_database, monkeypatch)
    monkeypatch.setattr(settings, "publishing_enabled", True)
    monkeypatch.setattr(settings, "publish_youtube_enabled", True)
    fake = FakeYouTube()
    install_transport(monkeypatch, fake)
    source_id = add_source("revoked")
    put_policy()
    await drain(isolated_database)
    put_policy(auto_publish=True, target_accounts={"youtube": [account_id]}, daily_publish_limits={"youtube": 5})
    worker.scheduler.scheduler_once(isolated_database)
    with TestClient(main.app) as client:
        assert client.post(f"/api/v1/sources/{source_id}/rights/revoke").status_code == 200
    await drain(isolated_database)
    with isolated_database() as db:
        publication = db.scalar(select(Publication))
        assert publication is None or publication.status in {"blocked", "failed", "cancelled"}
    assert fake.completed == [] and fake.inits == 0


@pytest.mark.asyncio
async def test_media_child_is_killed_on_cancellation(tmp_path, monkeypatch):
    """Sentinel P1-A follow-up: a hung media child is terminated, not leaked as a thread."""
    monkeypatch.setattr(settings, "media_terminate_grace_seconds", 0.5)
    task = asyncio.create_task(media_tasks.run_media_task(time.sleep, (3600,), tmp_path / "result.json"))
    await asyncio.sleep(0.2)
    children = [int(pid) for pid in os.listdir("/proc") if pid.isdigit() and _parent(int(pid)) == os.getpid()
                and b"resource_tracker" not in _cmdline(int(pid))]
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 3
    for pid in children:
        assert not _alive(pid), f"media child {pid} survived cancellation"


@pytest.mark.asyncio
async def test_media_child_failure_is_sanitized(tmp_path):
    with pytest.raises(media_tasks.MediaProcessingError):
        await media_tasks.run_media_task(os.path.getsize, ("/nonexistent/secret-path",), tmp_path / "result.json")
    assert await media_tasks.run_media_task(os.path.basename, ("/tmp/ok.mp4",), tmp_path / "result.json") == "ok.mp4"


def _parent(pid):
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return int(handle.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def _alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False
