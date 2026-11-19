"""Shared publishing policy, network boundary, and provider checkpoint contract."""
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

import httpx

from .config import settings

Platform = Literal["youtube", "tiktok", "instagram"]
RETRYABLE = {"transient", "rate_limited"}
TIKTOK_PRIVACY = {"SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "PUBLIC_TO_EVERYONE"}


class PublishingUnavailable(RuntimeError):
    def __init__(self, error_class: str, message: str, *, retry_after: int = 0):
        super().__init__(message)
        self.error_class = error_class
        self.retry_after = retry_after


def utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def provider_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(45, connect=10), follow_redirects=False, trust_env=False,
    )


def graph_url() -> str:
    return f"https://graph.facebook.com/{settings.meta_graph_version}"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def response_data(response: httpx.Response) -> dict:
    try:
        result = response.json()
    except ValueError:
        return {}
    return result if isinstance(result, dict) else {}


def provider_error(response: httpx.Response, *, oauth: bool = False) -> None:
    payload = response_data(response)
    error = payload.get("error") or {}
    details = error if isinstance(error, dict) else {}
    status = response.status_code
    code = details.get("code", error if isinstance(error, str) else "")
    reasons = {item.get("reason") for item in details.get("errors", []) if isinstance(item, dict)}
    if status == 429 or code in (4, 17, 32, 613, "rate_limit_exceeded", "spam_risk_too_many_posts") or reasons & {"quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"}:
        delay = response.headers.get("Retry-After", "")
        raise PublishingUnavailable("rate_limited", "Provider upload quota or rate limit reached", retry_after=min(int(delay), 86400) if delay.isascii() and delay.isdigit() and len(delay) <= 8 else 900)
    if oauth and code in ("invalid_grant", "access_token_invalid", 102, 190):
        raise PublishingUnavailable("auth_revoked", "Provider authorization expired or was revoked")
    if status == 401 or code in (102, 190, "access_token_invalid"):
        raise PublishingUnavailable("auth_required", "Provider rejected the access token")
    if status >= 500 or status in (408, 425) or details.get("is_transient") or code == "internal_error":
        raise PublishingUnavailable("transient", "Provider temporarily unavailable")
    if status == 403 or code in (10, 200, "scope_not_authorized", "unaudited_client_can_only_post_to_private_accounts", "spam_risk_user_banned_from_posting"):
        raise PublishingUnavailable("permission", "Provider denied publishing permissions")
    if not 200 <= status < 300 or (error and code != "ok"):
        raise PublishingUnavailable("invalid", "Provider rejected the request")


async def send(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    try:
        return await client.request(method, url, **kwargs)
    except httpx.RequestError:
        raise PublishingUnavailable("transient", "Provider connection interrupted") from None


def require_id(value) -> str:
    if not isinstance(value, (str, int)) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,255}", str(value)) or str(value) in {".", ".."}:
        raise PublishingUnavailable("invalid", "Provider response is missing a valid identifier")
    return str(value)


def upload_url(value, allowed_hosts: set[str]) -> str:
    try:
        if isinstance(value, str):
            parsed = httpx.URL(value)
            secret_query = {"access_token", "refresh_token", "authorization"}
            if (
                parsed.scheme == "https" and parsed.host in allowed_hosts and parsed.port in (None, 443)
                and not parsed.userinfo and not parsed.fragment
                and not secret_query.intersection(key.lower() for key in parsed.params)
            ):
                return value
    except httpx.InvalidURL:
        pass
    raise PublishingUnavailable("invalid", "Provider returned an invalid upload destination")


def validate_metadata(platform: str, title: str, description: str, privacy: str) -> None:
    if not title.strip() or len(title) > 100 or any(char in title for char in "<>"):
        raise PublishingUnavailable("invalid", "Title must contain 1–100 characters without angle brackets")
    if len(description.encode()) > 5000 or (platform == "youtube" and any(char in description for char in "<>")):
        raise PublishingUnavailable("invalid", "Description exceeds provider limits or contains angle brackets")
    if platform == "youtube" and privacy not in {"private", "unlisted"}:
        raise PublishingUnavailable("invalid", "YouTube privacy must be private or unlisted")
    if platform == "tiktok" and (privacy not in TIKTOK_PRIVACY or (privacy != "SELF_ONLY" and not settings.tiktok_app_audited)):
        raise PublishingUnavailable("invalid", "Requested TikTok visibility requires an audited app and supported privacy level")
    if platform == "instagram" and (privacy != "account" or len(description) > 2200):
        raise PublishingUnavailable("invalid", "Instagram requires account visibility and a caption of at most 2200 characters")


@dataclass(frozen=True)
class PublishResult:
    external_id: str | None
    external_url: str | None = None


@dataclass
class Upload:
    path: Path
    total: int
    title: str
    description: str
    privacy: str
    duration: float
    account_id: str
    save: Callable[[dict, int], None]
    checkpoint: dict = field(default_factory=dict)
    sent: int = 0

    def persist(self, sent: int, **changes) -> None:
        checkpoint = {**self.checkpoint, **changes}
        self.save(checkpoint, sent)
        self.checkpoint = checkpoint
        self.sent = sent


class PublishClient:
    def __init__(self, client: httpx.AsyncClient, token: str, authorize, refresh):
        self.client = client
        self.token = token
        self.authorize = authorize
        self.refresh = refresh
        self.refreshed = False

    async def request(self, method: str, url: str, *, auth: str = "Bearer", accepted: tuple = (), **kwargs) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        content_factory = kwargs.pop("content_factory", None)

        async def request_once():
            self.authorize()
            authorization = {"Authorization": f"{auth} {self.token}"} if auth else {}
            if content_factory is not None:
                kwargs["content"] = content_factory()
            return await send(self.client, method, url, headers={**headers, **authorization}, **kwargs)

        response = await request_once()
        try:
            if response.status_code not in accepted:
                provider_error(response)
        except PublishingUnavailable as exc:
            if exc.error_class != "auth_required" or not auth:
                raise
            if self.refreshed:
                raise PublishingUnavailable("auth_revoked", "Provider rejected refreshed authorization") from None
            self.refreshed = True
            self.token = await self.refresh()
            response = await request_once()
            if response.status_code not in accepted:
                try:
                    provider_error(response)
                except PublishingUnavailable as retry_error:
                    if retry_error.error_class == "auth_required":
                        raise PublishingUnavailable("auth_revoked", "Provider rejected refreshed authorization") from None
                    raise
        return response
