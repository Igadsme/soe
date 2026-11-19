"""https://developers.tiktok.com/docs/en/content-posting-api-get-started"""
import asyncio

from ..publishing import (
    PublishClient,
    PublishingUnavailable,
    PublishResult,
    Upload,
    require_id,
    response_data,
    upload_url,
    validate_metadata,
)

POST = "https://open.tiktokapis.com/v2/post/publish"
UPLOAD_HOSTS = {"upload.tiktokapis.com", "upload.us.tiktokapis.com", "upload.eu.tiktokapis.com", "open-upload.tiktokapis.com"}
POLL_DELAYS = (0, 2, 4, 8, 16, 30)
JSON_HEADERS = {"Content-Type": "application/json; charset=UTF-8"}


def chunk_plan(total: int) -> tuple[int, int]:
    # https://developers.tiktok.com/docs/en/content-posting-api-media-transfer-guide
    if not 0 < total <= 4_000_000_000:
        raise PublishingUnavailable("invalid", "TikTok video size is outside supported limits")
    if total <= 64_000_000:
        return total, 1
    chunk_size = 16_000_000
    return chunk_size, total // chunk_size


def data(response) -> dict:
    payload = response_data(response).get("data")
    if not isinstance(payload, dict):
        raise PublishingUnavailable("transient", "TikTok returned an incomplete response")
    return payload


async def post_status(client, publish_id: str) -> dict:
    # https://developers.tiktok.com/docs/en/content-posting-api-reference-get-video-status
    status = data(await client.request("POST", f"{POST}/status/fetch/", headers=JSON_HEADERS, json={"publish_id": publish_id}))
    if status.get("status") == "FAILED":
        reason = status.get("fail_reason")
        if reason in ("auth_removed", "token_expired"):
            raise PublishingUnavailable("auth_revoked", "TikTok authorization was removed")
        raise PublishingUnavailable("invalid", "TikTok rejected the submitted video")
    return status


def completed(status: dict, publish_id: str) -> PublishResult:
    ids = status.get("publicaly_available_post_id")
    return PublishResult(require_id(ids[0]) if isinstance(ids, list) and ids else publish_id)


async def publish(client: PublishClient, upload: Upload) -> PublishResult:
    validate_metadata("tiktok", upload.title, upload.description, upload.privacy)
    publish_id = upload.checkpoint.get("publish_id")
    chunk_size, total_chunks = chunk_plan(upload.total)
    if publish_id:
        status = await post_status(client, publish_id)
        if status.get("status") == "PUBLISH_COMPLETE":
            return completed(status, publish_id)
        if status.get("status") == "PROCESSING_UPLOAD":
            sent = status.get("uploaded_bytes")
            if not isinstance(sent, int) or not upload.sent <= sent <= upload.total or (sent != upload.total and (sent % chunk_size or sent >= chunk_size * total_chunks)):
                raise PublishingUnavailable("transient", "TikTok has not confirmed its upload offset")
            upload.persist(sent)
        elif status.get("status") == "PROCESSING_DOWNLOAD":
            upload.persist(upload.total)
        else:
            raise PublishingUnavailable("transient", "TikTok returned an unexpected publishing status")
    else:
        info = data(await client.request("POST", f"{POST}/creator_info/query/", headers=JSON_HEADERS))
        if upload.privacy not in (info.get("privacy_level_options") or []):
            raise PublishingUnavailable("permission", "TikTok creator does not allow this visibility")
        max_duration = info.get("max_video_post_duration_sec")
        if not isinstance(max_duration, (int, float)) or not 0 < upload.duration <= max_duration:
            raise PublishingUnavailable("invalid", "Video exceeds TikTok creator duration limits")
        result = data(await client.request("POST", f"{POST}/video/init/", headers=JSON_HEADERS, json={
            "post_info": {
                "title": upload.title,
                "privacy_level": upload.privacy,
                **{f"disable_{setting}": bool(info.get(f"{setting}_disabled", True)) for setting in ("comment", "duet", "stitch")},
            },
            "source_info": {"source": "FILE_UPLOAD", "video_size": upload.total, "chunk_size": chunk_size, "total_chunk_count": total_chunks},
        }))
        publish_id = require_id(result.get("publish_id"))
        location = upload_url(result.get("upload_url"), UPLOAD_HOSTS)
        upload.persist(0, publish_id=publish_id, url=location)

    if upload.sent < upload.total:
        location = upload_url(upload.checkpoint.get("url"), UPLOAD_HOSTS)
        with upload.path.open("rb") as media:
            while upload.sent < upload.total:
                start = upload.sent
                media.seek(start)
                size = chunk_size if start // chunk_size < total_chunks - 1 else upload.total - start
                chunk = media.read(size)
                if len(chunk) != size:
                    raise PublishingUnavailable("invalid", "Rendered media changed during upload")
                end = start + size
                result = await client.request("PUT", location, auth="", accepted=(403, 404, 416), headers={
                    "Content-Type": "video/mp4", "Content-Range": f"bytes {start}-{end - 1}/{upload.total}",
                }, content=chunk)
                if result.status_code == 416:
                    raise PublishingUnavailable("transient", "TikTok requested upload offset reconciliation")
                if result.status_code in (403, 404):
                    raise PublishingUnavailable("ambiguous", "TikTok upload session expired. Check the account before submitting again.")
                if result.status_code != (201 if end == upload.total else 206):
                    raise PublishingUnavailable("transient", "TikTok has not acknowledged the requested upload range")
                upload.persist(end)
    for delay in POLL_DELAYS:
        if delay:
            await asyncio.sleep(delay)
        status = await post_status(client, publish_id)
        if status.get("status") == "PUBLISH_COMPLETE":
            return completed(status, publish_id)
    raise PublishingUnavailable("transient", "TikTok is still processing the video")
