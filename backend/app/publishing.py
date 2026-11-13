"""Official publishing API boundaries. These clients never bypass OAuth or platform review."""
from dataclasses import dataclass
from pathlib import Path
import httpx


class PublishingUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class OAuthAccount:
    platform: str
    access_token: str
    account_id: str


class YouTubePublisher:
    async def upload(self, account: OAuthAccount, media_path: Path, title: str, description: str) -> str:
        if account.platform != "youtube":
            raise PublishingUnavailable("account platform mismatch")
        if not media_path.is_file():
            raise PublishingUnavailable("rendered media file does not exist")
        metadata = {"snippet": {"title": title, "description": description}, "status": {"privacyStatus": "private"}}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post("https://www.googleapis.com/upload/youtube/v3/videos?part=snippet,status", headers={"Authorization": f"Bearer {account.access_token}", "Content-Type": "application/json"}, json=metadata)
        if response.status_code in {401, 403}:
            raise PublishingUnavailable("YouTube OAuth token is invalid or lacks upload permission")
        response.raise_for_status()
        return response.json().get("id", "")


class TikTokPublisher:
    async def initialize_direct_post(self, account: OAuthAccount, title: str, video_size: int) -> dict:
        if account.platform != "tiktok":
            raise PublishingUnavailable("account platform mismatch")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post("https://open.tiktokapis.com/v2/post/publish/video/init/", headers={"Authorization": f"Bearer {account.access_token}", "Content-Type": "application/json"}, json={"post_info": {"title": title, "privacy_level": "SELF_ONLY"}, "source_info": {"source": "FILE_UPLOAD", "video_size": video_size}})
        if response.status_code in {401, 403}:
            raise PublishingUnavailable("TikTok OAuth token is invalid or lacks direct-post permission")
        response.raise_for_status()
        return response.json()


class InstagramPublisher:
    async def create_reel_container(self, account: OAuthAccount, video_url: str, caption: str) -> dict:
        if account.platform != "instagram":
            raise PublishingUnavailable("account platform mismatch")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"https://graph.facebook.com/v20.0/{account.account_id}/media", params={"media_type": "REELS", "video_url": video_url, "caption": caption, "access_token": account.access_token})
        if response.status_code in {401, 403}:
            raise PublishingUnavailable("Instagram token is invalid or lacks publishing permission")
        response.raise_for_status()
        return response.json()
