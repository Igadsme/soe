"""Publishing execution using the existing job lease and fenced checkpoints."""
import asyncio
import json
import random
from datetime import timedelta
from pathlib import Path

from cryptography.fernet import InvalidToken
from sqlalchemy import func, or_, select, update

from . import oauth, publishing
from .config import settings
from .media import sha256_file
from .models import Clip, ConnectedAccount, Job, Publication, now
from .publications import latest_rights, transition
from .publishers import instagram, tiktok, youtube
from .publishing import PublishingUnavailable
from .rights import can_publish_to
from .schemas import safe_media_path
from .token_vault import token_vault

PROVIDERS = {"youtube": youtube.publish, "tiktok": tiktok.publish, "instagram": instagram.publish}


class LeaseLost(RuntimeError):
    pass


class PublisherJob:
    def __init__(self, session_factory, ownership):
        self.sessions = session_factory
        self.ownership = ownership
        self.account_token = None

    def job(self, db, *, lock=False):
        if lock:
            # A conditional write fences stale owners on both SQLite and PostgreSQL.
            db.execute(update(Job).where(self.ownership()).values(locked_at=Job.locked_at))
        job = db.scalar(select(Job).where(self.ownership()))
        if job is None:
            raise LeaseLost
        return job

    def check_policy(self, db, job, row):
        if not settings.publishing_allowed(row.platform):
            raise PublishingUnavailable("disabled", "Publishing is disabled for this platform")
        rights = latest_rights(db, job.source_id)
        if not rights or not can_publish_to(rights, row.platform):
            raise PublishingUnavailable("rights", "Current rights do not authorize publication")
        account = db.get(ConnectedAccount, row.account_id)
        if account is None or account.status != "connected" or account.platform != row.platform or not account.access_token:
            raise PublishingUnavailable("auth_revoked", "The publishing account is disconnected")
        return account

    def prepare(self):
        with self.sessions() as db:
            job = self.job(db, lock=True)
            row = db.get(Publication, job.publication_id)
            account = self.check_policy(db, job, row)
            if row.scheduled_at and publishing.utc(row.scheduled_at) > now():
                raise PublishingUnavailable("transient", "Publication is scheduled for a later time")
            clip = db.get(Clip, row.clip_id)
            if clip is None or clip.source_id != job.source_id:
                raise PublishingUnavailable("invalid", "Rendered clip is unavailable")
            publishing.validate_metadata(row.platform, row.title, row.description, row.privacy_status)
            row.attempts = job.attempts
            row.next_attempt_at = None
            row.error_class = row.last_error = None
            transition(db, row, "running", "worker")
            db.commit()
            return row, clip, account

    def reserve(self, dry_run: bool):
        with self.sessions() as db:
            job = self.job(db, lock=True)
            row = db.get(Publication, job.publication_id)
            # Serialize quota reservations and upstream token refresh for one account.
            db.execute(update(ConnectedAccount).where(ConnectedAccount.id == row.account_id)
                       .values(updated_at=ConnectedAccount.updated_at))
            account = self.check_policy(db, job, row)
            self.account_token = account.access_token
            others = (Publication.account_id == account.id) & (Publication.id != row.id)
            cutoff = now() - timedelta(hours=24)
            recent = or_(Publication.published_at >= cutoff,
                         (Publication.upload_started_at >= cutoff) & Publication.published_at.is_(None))
            count = db.scalar(select(func.count()).select_from(Publication).where(others, recent))
            if row.upload_started_at is None and count >= account.daily_upload_limit:
                earliest = db.scalar(select(func.min(func.coalesce(Publication.published_at, Publication.upload_started_at)))
                                     .where(others, recent))
                delay = max(60, int((publishing.utc(earliest) - cutoff).total_seconds()) + 1) if earliest else 86400
                raise PublishingUnavailable("rate_limited", "Account daily upload limit reached", retry_after=delay)
            in_flight = db.scalar(select(Publication.id).join(Job, Job.publication_id == Publication.id).where(
                others, Publication.upload_started_at.is_not(None), Job.status == "running", Job.lease_expires_at > now(),
            ).limit(1))
            if in_flight:
                raise PublishingUnavailable("transient", "Another upload is running for this account", retry_after=60)
            if not dry_run and row.upload_started_at is None:
                row.upload_started_at = now()
            db.commit()
            return account

    def authorize(self):
        with self.sessions() as db:
            job = self.job(db)
            row = db.get(Publication, job.publication_id)
            account = self.check_policy(db, job, row)
            if settings.publishing_dry_run:
                raise PublishingUnavailable("disabled", "Publishing switched to dry-run")
            if account.access_token != self.account_token:
                raise PublishingUnavailable("transient", "Account credentials changed during upload")

    def save(self, checkpoint: dict, sent: int):
        with self.sessions() as db:
            job = self.job(db, lock=True)
            row = db.get(Publication, job.publication_id)
            row.upload_checkpoint = token_vault().encrypt(json.dumps(checkpoint).encode()).decode()
            row.bytes_sent = sent
            db.commit()

    async def refresh(self, client, account: ConnectedAccount) -> str:
        self.authorize()
        credentials = await oauth.refresh(client, account)
        with self.sessions() as db:
            self.job(db, lock=True)
            current = db.scalar(select(ConnectedAccount).where(ConnectedAccount.id == account.id).with_for_update())
            if current.status != "connected" or current.access_token != self.account_token:
                raise PublishingUnavailable("transient", "Account credentials changed during refresh")
            oauth.store_credentials(current, credentials)
            db.commit()
            self.account_token = current.access_token
            account.access_token = current.access_token
            account.refresh_token = current.refresh_token
        return credentials.access

    async def execute(self):
        try:
            row, clip, _ = self.prepare()
            path = Path(safe_media_path(clip.output_path or ""))
            if not path.is_file() or path.stat().st_size != row.bytes_total or await asyncio.to_thread(sha256_file, path) != row.media_sha256:
                raise PublishingUnavailable("invalid", "Rendered media changed since publication was queued")
            dry_run = settings.publishing_dry_run or row.dry_run
            if row.platform == "tiktok":
                tiktok.chunk_plan(row.bytes_total)
            account = self.reserve(dry_run)
            vault = token_vault()
            access = vault.decrypt(account.access_token.encode()).decode()
            state = json.loads(vault.decrypt(row.upload_checkpoint.encode())) if row.upload_checkpoint else {}
            if dry_run:
                if state:
                    raise PublishingUnavailable("disabled", "Upload reconciliation is paused during dry-run")
                self.finish(dry_run=True)
                return
            async with publishing.provider_client() as client:
                async def refresh():
                    return await self.refresh(client, account)
                expires_soon = account.expires_at and publishing.utc(account.expires_at) <= now() + timedelta(seconds=90)
                if expires_soon:
                    access = await refresh()
                api = publishing.PublishClient(client, access, self.authorize, refresh)
                upload = publishing.Upload(path, row.bytes_total, row.title, row.description, row.privacy_status,
                                           clip.end_seconds - clip.start_seconds, account.external_account_id, self.save,
                                           state, row.bytes_sent)
                result = await PROVIDERS[row.platform](api, upload)
            self.finish(result=result)
        except LeaseLost:
            return
        except PublishingUnavailable as exc:
            self.finish(error=exc)
        except (InvalidToken, ValueError):
            self.finish(error=PublishingUnavailable("configuration", "Publishing credentials or media path are invalid"))
        except OSError:
            self.finish(error=PublishingUnavailable("transient", "Rendered media could not be read"))

    def finish(self, result=None, error=None, *, dry_run=False):
        with self.sessions() as db:
            try:
                job = self.job(db, lock=True)
            except LeaseLost:
                return
            row = db.get(Publication, job.publication_id)
            row.attempts = job.attempts
            job.locked_by = None
            job.lease_expires_at = None
            row.next_attempt_at = None
            if error is None:
                job.status = "succeeded"
                job.last_error = row.error_class = row.last_error = None
                if dry_run:
                    row.dry_run = True
                else:
                    row.external_id, row.external_url = result.external_id, result.external_url
                    row.bytes_sent = row.bytes_total
                    row.published_at = now()
                    row.upload_checkpoint = None
                transition(db, row, "dry_run" if dry_run else "succeeded", "worker")
            else:
                row.error_class, row.last_error = error.error_class, str(error)
                job.last_error = row.last_error
                if not row.upload_checkpoint:
                    row.upload_started_at = None
                if error.error_class == "auth_revoked" and self.account_token:
                    account = db.scalar(select(ConnectedAccount).where(ConnectedAccount.id == row.account_id).with_for_update())
                    if account and account.access_token == self.account_token:
                        oauth.mark_revoked(account)
                if error.error_class in publishing.RETRYABLE and job.attempts < job.max_attempts:
                    delay = max(error.retry_after, min(300, 2 ** job.attempts) + random.uniform(0, 2))  # noqa: S311
                    job.status = "queued"
                    job.run_after = row.next_attempt_at = now() + timedelta(seconds=delay)
                    transition(db, row, "queued", "worker")
                else:
                    job.status = "failed"
                    transition(db, row, "blocked" if error.error_class == "rights" else "failed", "worker")
            db.commit()
