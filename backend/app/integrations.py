"""Official metadata provider adapters. No adapter treats public visibility as reuse permission."""
from dataclasses import dataclass
import httpx
from .config import settings


@dataclass(frozen=True)
class DiscoveredItem:
    platform: str
    external_id: str
    title: str
    url: str
    metadata: dict


class ProviderUnavailable(RuntimeError):
    pass


class YouTubeDataProvider:
    async def search(self, query: str, max_results: int = 10) -> list[DiscoveredItem]:
        if not settings.youtube_api_key:
            raise ProviderUnavailable("YOUTUBE_API_KEY is not configured")
        params = {"part": "snippet", "q": query, "type": "video", "maxResults": max_results, "key": settings.youtube_api_key}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get("https://www.googleapis.com/youtube/v3/search", params=params)
            response.raise_for_status()
            body = response.json()
        return [DiscoveredItem("youtube", row["id"]["videoId"], row["snippet"]["title"], f"https://www.youtube.com/watch?v={row['id']['videoId']}", row["snippet"]) for row in body.get("items", [])]


class TwitchHelixProvider:
    async def search(self, query: str, max_results: int = 10) -> list[DiscoveredItem]:
        if not settings.twitch_client_id or not settings.twitch_client_secret:
            raise ProviderUnavailable("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are not configured")
        async with httpx.AsyncClient(timeout=20) as client:
            token = await client.post("https://id.twitch.tv/oauth2/token", params={"client_id": settings.twitch_client_id, "client_secret": settings.twitch_client_secret, "grant_type": "client_credentials"})
            token.raise_for_status()
            access_token = token.json()["access_token"]
            response = await client.get("https://api.twitch.tv/helix/search/channels", params={"query": query, "first": max_results}, headers={"Client-ID": settings.twitch_client_id, "Authorization": f"Bearer {access_token}"})
            response.raise_for_status()
            body = response.json()
        return [DiscoveredItem("twitch", row["id"], row["display_name"], f"https://www.twitch.tv/{row['broadcaster_login']}", row) for row in body.get("data", [])]


class FixtureProvider:
    """Explicitly non-production fixture provider for deterministic integration tests."""
    def __init__(self, platform: str = "youtube"):
        self.platform = platform

    async def search(self, query: str, max_results: int = 10) -> list[DiscoveredItem]:
        return [DiscoveredItem(self.platform, f"fixture-{query}-{i}", f"Fixture {query} #{i}", f"https://example.invalid/{self.platform}/{i}", {"fixture": True, "query": query}) for i in range(min(max_results, 3))]
