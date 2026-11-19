"""Official metadata provider adapters. No adapter treats public visibility as reuse permission."""
from dataclasses import dataclass
from functools import wraps

import httpx

from .config import secret_value, settings


@dataclass(frozen=True)
class DiscoveredItem:
    platform: str
    external_id: str
    title: str
    url: str
    metadata: dict


class ProviderUnavailable(RuntimeError):
    status_code = 503


class ProviderBadResponse(ProviderUnavailable):
    status_code = 502


def provider_errors(method):
    @wraps(method)
    async def wrapped(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderBadResponse("provider returned invalid data") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable("provider request failed") from exc
    return wrapped


class YouTubeDataProvider:
    @provider_errors
    async def search(self, query: str, max_results: int = 10) -> list[DiscoveredItem]:
        key = youtube_key()
        params = {"part": "snippet", "q": query, "type": "video", "maxResults": max_results}
        async with metadata_client() as client:
            # API key travels in the documented X-Goog-Api-Key header, never in the URL/query string.
            response = await client.get("https://www.googleapis.com/youtube/v3/search", params=params,
                                        headers={"X-Goog-Api-Key": key})
            response.raise_for_status()
            body = response.json()
        return [DiscoveredItem("youtube", row["id"]["videoId"], row["snippet"]["title"], f"https://www.youtube.com/watch?v={row['id']['videoId']}", row["snippet"]) for row in body.get("items", [])]


class TwitchHelixProvider:
    @provider_errors
    async def search(self, query: str, max_results: int = 10) -> list[DiscoveredItem]:
        async with metadata_client() as client:
            headers = await twitch_headers(client)
            rows = await helix(client, "search/channels", {"query": query, "first": max_results}, headers)
        return [DiscoveredItem("twitch", row["id"], row["display_name"], f"https://www.twitch.tv/{row['broadcaster_login']}", row) for row in rows]


class FixtureProvider:
    """Explicitly non-production fixture provider for deterministic integration tests."""
    def __init__(self, platform: str = "youtube"):
        self.platform = platform

    @provider_errors
    async def search(self, query: str, max_results: int = 10) -> list[DiscoveredItem]:
        return [DiscoveredItem(self.platform, f"fixture-{query}-{i}", f"Fixture {query} #{i}", f"https://example.invalid/{self.platform}/{i}", {"fixture": True, "query": query}) for i in range(min(max_results, 3))]


def metadata_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False)


class ProviderNotConfigured(ProviderUnavailable):
    pass


def youtube_key() -> str:
    key = secret_value(settings.youtube_api_key).strip()
    if not key:
        raise ProviderNotConfigured("YouTube Data API is not configured")
    return key


async def youtube_items(client, resource: str, *, key: str, **params) -> list[dict]:
    response = await client.get(f"https://www.googleapis.com/youtube/v3/{resource}", params=params,
                                headers={"X-Goog-Api-Key": key})
    response.raise_for_status()
    return response.json()["items"]


class YouTubeDiscovery(YouTubeDataProvider):
    @provider_errors
    async def discover(self, query: str, channels: list[str], limit: int) -> list[DiscoveredItem]:
        key = youtube_key()
        if not channels:
            matches = await self.search(query, limit)
            ids = [item.external_id for item in matches]
        else:
            ids = []
            async with metadata_client() as client:
                rows = await youtube_items(client, "channels", part="contentDetails", id=",".join(channels), key=key)
                for row in rows:
                    playlist = row["contentDetails"]["relatedPlaylists"]["uploads"]
                    items = await youtube_items(client, "playlistItems", part="contentDetails",
                                                playlistId=playlist, maxResults=limit, key=key)
                    ids.extend(item["contentDetails"]["videoId"] for item in items)
        results = []
        async with metadata_client() as client:
            for offset in range(0, len(ids), 50):
                videos = await youtube_items(client, "videos", part="snippet,contentDetails,statistics",
                                             id=",".join(ids[offset:offset + 50]), key=key)
                results.extend(DiscoveredItem("youtube", row["id"], row["snippet"]["title"],
                    f"https://www.youtube.com/watch?v={row['id']}", {
                        **row["snippet"], "duration": row["contentDetails"]["duration"],
                        "view_count": row.get("statistics", {}).get("viewCount"),
                    }) for row in videos if not channels or row["snippet"]["channelId"] in channels)
        return results


async def twitch_headers(client) -> dict[str, str]:
    if not settings.twitch_client_id or not secret_value(settings.twitch_client_secret).strip():
        raise ProviderNotConfigured("Twitch Helix is not configured")
    # Client secret goes in the form-encoded POST body (not the query string) so it never appears in URLs/logs.
    response = await client.post("https://id.twitch.tv/oauth2/token", data={
        "client_id": settings.twitch_client_id, "client_secret": secret_value(settings.twitch_client_secret),
        "grant_type": "client_credentials",
    })
    response.raise_for_status()
    return {"Client-ID": settings.twitch_client_id, "Authorization": f"Bearer {response.json()['access_token']}"}


async def helix(client, resource: str, params, headers: dict[str, str]) -> list[dict]:
    response = await client.get(f"https://api.twitch.tv/helix/{resource}", params=params, headers=headers)
    response.raise_for_status()
    return response.json()["data"]


class TwitchDiscovery(TwitchHelixProvider):
    @provider_errors
    async def discover(self, query: str, channels: list[str], limit: int) -> list[DiscoveredItem]:
        if not channels:
            return await self.search(query, limit)
        logins = {channel.lower() for channel in channels}
        async with metadata_client() as client:
            headers = await twitch_headers(client)
            users = await helix(client, "users", [("login", login) for login in sorted(logins)], headers)
            streams = await helix(client, "streams", [("user_login", login) for login in sorted(logins)], headers)
            results = [
                DiscoveredItem("twitch", f"stream:{row['id']}", row["title"] or row["user_name"],
                               f"https://www.twitch.tv/{row['user_login']}", row)
                for row in streams if row["user_login"].lower() in logins
            ]
            for user in users:
                if user["login"].lower() not in logins:
                    continue
                archives = await helix(client, "videos", {
                    "user_id": user["id"], "type": "archive", "sort": "time", "first": limit,
                }, headers)
                results.extend(DiscoveredItem("twitch", f"vod:{row['id']}", row["title"],
                    f"https://www.twitch.tv/videos/{row['id']}", row) for row in archives)
        return results
