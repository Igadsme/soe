"""Provider-specific OAuth contracts and encrypted account credentials."""
import base64
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from hashlib import sha256
from urllib.parse import urlencode

from .config import settings
from .models import ConnectedAccount, now
from .publishing import (
    PublishingUnavailable,
    bearer,
    graph_url,
    provider_error,
    require_id,
    response_data,
    send,
)
from .token_vault import token_vault

GOOGLE_EXCHANGE_URL = "https://oauth2.googleapis.com/token"
TIKTOK_EXCHANGE_URL = "https://open.tiktokapis.com/v2/oauth/token/"
SCOPES = {
    # channels.list mine=true needs a read scope. Upload alone does not authorize it.
    "youtube": ("https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"),
    "tiktok": ("video.publish", "user.info.basic"),
    "instagram": ("instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement"),
}


@dataclass(frozen=True)
class Credentials:
    access: str = field(repr=False)
    refresh: str | None = field(default=None, repr=False)
    expires_in: int | None = None
    scopes: tuple[str, ...] = ()


def redirect_uri(platform: str) -> str:
    return f"{settings.oauth_origin()}/api/v1/oauth/{platform}/callback"


def configured(platform: str) -> bool:
    return bool(settings.oauth_origin() and all(settings.oauth_credentials(platform)))


def authorize(platform: str, state: str) -> tuple[str, str | None]:
    identifier, _ = settings.oauth_credentials(platform)
    verifier = None
    params = {"redirect_uri": redirect_uri(platform), "state": state, "response_type": "code"}
    if platform == "youtube":
        # https://developers.google.com/identity/protocols/oauth2/web-server
        endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
        verifier = secrets.token_urlsafe(64)
        digest = base64.urlsafe_b64encode(sha256(verifier.encode()).digest()).decode().rstrip("=")
        params.update(client_id=identifier, scope=" ".join(SCOPES[platform]), code_challenge=digest,
                      code_challenge_method="S256", access_type="offline", prompt="consent")
    elif platform == "tiktok":
        # https://developers.tiktok.com/docs/en/login-kit-overview
        # TikTok's confidential Web client uses state. Its PKCE contract is for desktop/mobile.
        endpoint = "https://www.tiktok.com/v2/auth/authorize/"
        params.update(client_key=identifier, scope=",".join(SCOPES[platform]))
    else:
        # https://developers.facebook.com/docs/facebook-login/guides/advanced/manual-flow/
        endpoint = f"https://www.facebook.com/{settings.meta_graph_version}/dialog/oauth"
        params.update(client_id=identifier, scope=",".join(SCOPES[platform]))
    return f"{endpoint}?{urlencode(params)}", verifier


async def request(client, method: str, url: str, **kwargs) -> dict:
    response = await send(client, method, url, **kwargs)
    provider_error(response, oauth=True)
    return response_data(response)


def client_fields(platform: str) -> dict:
    identifier, secret = settings.oauth_credentials(platform)
    return {"client_key" if platform == "tiktok" else "client_id": identifier, "client_secret": secret}


def token_fields(payload: dict) -> Credentials:
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise PublishingUnavailable("invalid", "Provider response did not include an access token")
    refresh = payload.get("refresh_token")
    ttl = payload.get("expires_in")
    scopes = payload.get("scope")
    return Credentials(access, refresh if isinstance(refresh, str) else None,
                       ttl if isinstance(ttl, int) and 0 < ttl <= 31536000 else None,
                       tuple(scopes.replace(",", " ").split()) if isinstance(scopes, str) else ())


async def exchange(client, platform: str, code: str, verifier: str | None):
    form = {**client_fields(platform), "code": code, "redirect_uri": redirect_uri(platform)}
    if platform != "instagram":
        form["grant_type"] = "authorization_code"
    if verifier:
        form["code_verifier"] = verifier
    endpoint = {"youtube": GOOGLE_EXCHANGE_URL, "tiktok": TIKTOK_EXCHANGE_URL, "instagram": f"{graph_url()}/oauth/access_token"}[platform]
    tokens = token_fields(await request(client, "POST", endpoint, data=form,
                                       headers={"Cache-Control": "no-cache"} if platform == "tiktok" else {}))
    if tokens.scopes and not set(SCOPES[platform]) <= set(tokens.scopes):
        raise PublishingUnavailable("permission", "The connected account did not grant all required permissions")
    if platform == "youtube":
        # https://developers.google.com/youtube/v3/docs/channels/list
        result = await request(client, "GET", "https://www.googleapis.com/youtube/v3/channels",
                               params={"part": "snippet", "mine": "true"}, headers=bearer(tokens.access))
        items = result.get("items") or []
        if len(items) != 1:
            raise PublishingUnavailable("permission", "A single YouTube channel is required")
        return require_id(items[0].get("id")), (items[0].get("snippet") or {}).get("title"), tokens
    if platform == "tiktok":
        # https://developers.tiktok.com/doc/tiktok-api-v2-get-user-info/
        result = await request(client, "GET", "https://open.tiktokapis.com/v2/user/info/",
                               params={"fields": "open_id,display_name"}, headers=bearer(tokens.access))
        user = (result.get("data") or {}).get("user") or {}
        return require_id(user.get("open_id")), user.get("display_name"), tokens
    long_lived = await meta_long_lived(client, tokens.access)
    return await meta_account(client, long_lived)


async def meta_long_lived(client, user_token: str) -> Credentials:
    # https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived/
    # Submit exchange parameters in the form body so credentials stay out of URLs.
    return token_fields(await request(client, "POST", f"{graph_url()}/oauth/access_token", data={
        **client_fields("instagram"), "grant_type": "fb_exchange_token", "fb_exchange_token": user_token,
    }))


async def meta_account(client, user: Credentials, account_id: str | None = None):
    # https://developers.facebook.com/docs/instagram-platform/content-publishing/
    # A linked instagram_business_account identifies a professional account.
    # Facebook Login publishing uses the Page token delivered by the accounts edge.
    params = {"fields": "id,access_token,instagram_business_account{id,username}"}
    matches = {}
    for _ in range(10):
        result = await request(client, "GET", f"{graph_url()}/me/accounts", params=params, headers=bearer(user.access))
        for page in result.get("data") or []:
            linked = page.get("instagram_business_account") or {}
            if linked.get("id") and page.get("access_token") and (account_id is None or str(linked["id"]) == account_id):
                matches[require_id(linked["id"])] = (linked.get("username"), page["access_token"])
        paging = result.get("paging") or {}
        cursor = (paging.get("cursors") or {}).get("after")
        if not paging.get("next"):
            break
        if not cursor:
            raise PublishingUnavailable("transient", "Meta returned incomplete account pagination")
        params["after"] = cursor
    else:
        raise PublishingUnavailable("permission", "Too many linked Facebook Pages to select an Instagram account")
    if len(matches) != 1:
        raise PublishingUnavailable("permission", "A single eligible Instagram professional account is required")
    external_id, (name, page_token) = next(iter(matches.items()))
    return external_id, name, Credentials(page_token, user.access, user.expires_in or 60 * 86400)


def store_credentials(account: ConnectedAccount, credentials: Credentials) -> None:
    vault = token_vault()
    account.access_token = vault.encrypt(credentials.access.encode()).decode()
    if credentials.refresh:
        account.refresh_token = vault.encrypt(credentials.refresh.encode()).decode()
    account.expires_at = now() + timedelta(seconds=credentials.expires_in) if credentials.expires_in else None
    if credentials.scopes or not account.scopes:
        account.scopes = list(credentials.scopes or SCOPES[account.platform])
    account.status = "connected"


def mark_revoked(account: ConnectedAccount) -> None:
    account.status = "revoked"
    account.access_token = account.refresh_token = None
    account.expires_at = None


async def refresh(client, account: ConnectedAccount) -> Credentials:
    if not account.refresh_token:
        raise PublishingUnavailable("auth_revoked", "Reconnect the account to refresh authorization")
    if not all(settings.oauth_credentials(account.platform)):
        raise PublishingUnavailable("disabled", "Platform OAuth client is not configured")
    token = token_vault().decrypt(account.refresh_token.encode()).decode()
    try:
        if account.platform == "instagram":
            # Re-exchange while valid. If Facebook no longer accepts the user token,
            # the operator must reconnect. There is no Facebook Login refresh_token grant.
            user = await meta_long_lived(client, token)
            return (await meta_account(client, user, account.external_account_id))[2]
        endpoint = GOOGLE_EXCHANGE_URL if account.platform == "youtube" else TIKTOK_EXCHANGE_URL
        response = await request(client, "POST", endpoint, data={
            **client_fields(account.platform), "grant_type": "refresh_token", "refresh_token": token,
        }, headers={"Cache-Control": "no-cache"} if account.platform == "tiktok" else {})
        return token_fields(response)
    except PublishingUnavailable as exc:
        if exc.error_class in {"auth_required", "auth_revoked"} or (account.platform == "instagram" and exc.error_class == "permission"):
            raise PublishingUnavailable("auth_revoked", "Provider authorization expired or was revoked") from None
        raise


async def revoke(client, account: ConnectedAccount) -> None:
    if not account.access_token:
        return
    vault = token_vault()
    access = vault.decrypt(account.access_token.encode()).decode()
    refresh_token = vault.decrypt(account.refresh_token.encode()).decode() if account.refresh_token else None
    if account.platform == "youtube":
        # https://developers.google.com/identity/protocols/oauth2/web-server#tokenrevoke
        await request(client, "POST", "https://oauth2.googleapis.com/revoke", data={"token": refresh_token or access})
    elif account.platform == "tiktok":
        # https://developers.tiktok.com/docs/en/oauth-revoke-user-access-token
        await request(client, "POST", "https://open.tiktokapis.com/v2/oauth/revoke/",
                      data={**client_fields("tiktok"), "token": access}, headers={"Cache-Control": "no-cache"})
    else:
        # https://developers.facebook.com/docs/graph-api/reference/user/permissions/#deleting
        await request(client, "DELETE", f"{graph_url()}/me/permissions", headers=bearer(refresh_token or access))
