"""https://developers.google.com/youtube/data/guides/using_resumable_upload_protocol"""
import re

from ..config import settings
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

UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"


def offset(response, total: int) -> int:
    value = response.headers.get("Range")
    if value is None:
        return 0
    match = re.fullmatch(r"bytes=0-([0-9]{1,15})", value)
    if not match or int(match[1]) >= total:
        raise PublishingUnavailable("transient", "YouTube returned an invalid upload range")
    return int(match[1]) + 1


def completed(response) -> PublishResult:
    video_id = require_id(response_data(response).get("id"))
    return PublishResult(video_id, f"https://www.youtube.com/watch?v={video_id}")


async def publish(client: PublishClient, upload: Upload) -> PublishResult:
    validate_metadata("youtube", upload.title, upload.description, upload.privacy)
    location = upload.checkpoint.get("url")
    if location:
        location = upload_url(location, {"www.googleapis.com", "upload.youtube.com"})
        response = await client.request("PUT", location, accepted=(308, 404, 410), headers={
            "Content-Length": "0", "Content-Range": f"bytes */{upload.total}",
        }, content=b"")
        if response.status_code in (404, 410):
            raise PublishingUnavailable("ambiguous", "YouTube upload session expired. Check the channel before submitting again.")
        if response.status_code != 308:
            return completed(response)
        upload.persist(offset(response, upload.total))
    else:
        # videos.insert publishAt always makes a private video public. The durable job
        # handles scheduling while preserving private or unlisted visibility.
        response = await client.request("POST", UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"}, headers={
            "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(upload.total),
        }, json={
            "snippet": {"title": upload.title, "description": upload.description, "categoryId": "22"},
            "status": {"privacyStatus": upload.privacy, "selfDeclaredMadeForKids": False},
        })
        location = upload_url(response.headers.get("Location"), {"www.googleapis.com", "upload.youtube.com"})
        upload.persist(0, url=location)

    with upload.path.open("rb") as media:
        while upload.sent < upload.total:
            start = upload.sent
            media.seek(start)
            chunk = media.read(min(settings.youtube_upload_chunk_bytes, upload.total - start))
            if not chunk:
                raise PublishingUnavailable("invalid", "Rendered media changed during upload")
            end = start + len(chunk) - 1
            response = await client.request("PUT", location, accepted=(308, 404, 410), headers={
                "Content-Type": "video/mp4", "Content-Range": f"bytes {start}-{end}/{upload.total}",
            }, content=chunk)
            if response.status_code in (404, 410):
                raise PublishingUnavailable("ambiguous", "YouTube upload session expired. Check the channel before submitting again.")
            if response.status_code != 308:
                return completed(response)
            sent = offset(response, upload.total)
            if not start < sent <= end + 1:
                raise PublishingUnavailable("transient", "YouTube has not acknowledged the requested upload range")
            upload.persist(sent)
    raise PublishingUnavailable("transient", "YouTube has not confirmed the completed upload")
