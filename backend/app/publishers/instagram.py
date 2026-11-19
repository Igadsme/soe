"""https://developers.facebook.com/docs/instagram-platform/content-publishing/"""
import asyncio

from ..config import settings
from ..publishing import (
    PublishClient,
    PublishingUnavailable,
    PublishResult,
    Upload,
    graph_url,
    require_id,
    response_data,
    validate_metadata,
)

POLL_DELAYS = (0, 60, 60, 60, 60)


async def container_status(client, container: str) -> str:
    response = await client.request("GET", f"{graph_url()}/{container}", params={"fields": "status_code,status"})
    status = response_data(response).get("status_code")
    if status in ("ERROR", "EXPIRED"):
        raise PublishingUnavailable("invalid", "Instagram rejected the media or the container expired")
    return status


async def publish(client: PublishClient, upload: Upload) -> PublishResult:
    validate_metadata("instagram", upload.title, upload.description, upload.privacy)
    account = f"{graph_url()}/{require_id(upload.account_id)}"
    container = upload.checkpoint.get("container")
    if not container:
        # https://developers.facebook.com/docs/graph-api/reference/ig-user/content_publishing_limit/
        payload = response_data(await client.request("GET", f"{account}/content_publishing_limit", params={"fields": "quota_usage,config"}))
        limits = payload.get("data") or []
        limit = limits[0] if limits else {}
        used = limit.get("quota_usage")
        allowed = (limit.get("config") or {}).get("quota_total")
        if not isinstance(used, int) or not isinstance(allowed, int):
            raise PublishingUnavailable("transient", "Instagram publishing quota is unavailable")
        if used >= allowed:
            raise PublishingUnavailable("rate_limited", "Instagram publishing quota reached", retry_after=3600)
        result = await client.request("POST", f"{account}/media", data={
            "media_type": "REELS", "upload_type": "resumable", "caption": upload.description,
        })
        container = require_id(response_data(result).get("id"))
        upload.persist(0, container=container)
    if not upload.checkpoint.get("transfer_started"):
        # Record intent before sending bytes. On interruption, reconcile via the
        # same container. Meta does not document an authoritative Reel byte offset.
        upload.persist(0, transfer_started=True)

        async def body():
            with upload.path.open("rb") as media:
                while chunk := media.read(1024 * 1024):
                    yield chunk

        # Use a replayable byte stream because a 401 may require one refresh.
        # Streaming requests are split out so each retry opens the file again.
        await upload_reel(client, container, upload.total, body)
        upload.persist(upload.total)
    for delay in POLL_DELAYS:
        if delay:
            await asyncio.sleep(delay)
        status = await container_status(client, container)
        if status == "PUBLISHED":
            return PublishResult(None)
        if status == "FINISHED" and not upload.checkpoint.get("finalize_started"):
            upload.persist(upload.total, finalize_started=True)
            try:
                result = await client.request("POST", f"{account}/media_publish", data={"creation_id": container})
            except PublishingUnavailable as exc:
                if exc.error_class == "rate_limited":
                    upload.persist(upload.total, finalize_started=False)
                raise
            return PublishResult(require_id(response_data(result).get("id")))
    raise PublishingUnavailable("transient", "Instagram has not yet confirmed this container")


async def upload_reel(client, container, length, body):
    url = f"https://rupload.facebook.com/ig-api-upload/{settings.meta_graph_version}/{container}"
    response = await client.request("POST", url, auth="OAuth", headers={
        "offset": "0", "file_size": str(length), "Content-Length": str(length), "Content-Type": "application/octet-stream",
    }, content_factory=body)
    if response_data(response).get("success") is not True:
        raise PublishingUnavailable("transient", "Instagram has not acknowledged the transfer")
