"""Operator-session-bound OAuth routes."""
import secrets
from datetime import timedelta
from hashlib import sha256

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import oauth, publishing
from .auth import require_auth, verify_session
from .config import settings
from .db import session
from .models import ConnectedAccount, OAuthState, now
from .token_vault import token_vault

router = APIRouter(prefix="/api/v1")
PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def binding(request: Request) -> str:
    cookie = request.cookies.get("soe_session")
    if not cookie:
        raise HTTPException(401, "OAuth requires an operator login session")
    verify_session(cookie)
    return sha256(cookie.encode()).hexdigest()


def redirect(platform: str, success: bool) -> RedirectResponse:
    origin = settings.oauth_origin() or ""
    return RedirectResponse(f"{origin}/?view=settings&oauth={'success' if success else 'failure'}&platform={platform}",
                            status_code=303, headers=PRIVATE_HEADERS)


@router.get("/oauth/{platform}/start")
def start(platform: publishing.Platform, session_binding: str = Depends(binding), db: Session = Depends(session)):
    if not oauth.configured(platform):
        raise HTTPException(409, "OAuth platform not configured")
    try:
        token_vault()
    except ValueError:
        raise HTTPException(409, "OAuth platform not configured") from None
    state = secrets.token_urlsafe(32)
    url, verifier = oauth.authorize(platform, state)
    timestamp = now()
    db.execute(delete(OAuthState).where(OAuthState.expires_at <= timestamp))
    db.add(OAuthState(state=state, session_binding=session_binding, platform=platform,
                      code_verifier=verifier, expires_at=timestamp + timedelta(minutes=10)))
    db.commit()
    return JSONResponse({"authorization_url": url}, headers=PRIVATE_HEADERS)


@router.get("/oauth/{platform}/callback")
async def callback(platform: publishing.Platform, state: str = "", code: str = "", error: str = "",
                   session_binding: str = Depends(binding), db: Session = Depends(session)):
    if not oauth.configured(platform) or not 1 <= len(state) <= 64:
        return redirect(platform, False)
    try:
        token_vault()
    except ValueError:
        return redirect(platform, False)
    timestamp = now()
    result = db.execute(update(OAuthState).where(
        OAuthState.state == state, OAuthState.session_binding == session_binding,
        OAuthState.platform == platform, OAuthState.expires_at > timestamp, OAuthState.consumed_at.is_(None),
    ).values(consumed_at=timestamp).returning(OAuthState.code_verifier)).first()
    db.commit()
    if result is None or error or not 1 <= len(code) <= 4096:
        return redirect(platform, False)
    try:
        async with publishing.provider_client() as client:
            external_id, name, credentials = await oauth.exchange(client, platform, code, result[0])
        account = db.scalar(select(ConnectedAccount).where(ConnectedAccount.platform == platform,
                                                          ConnectedAccount.external_account_id == external_id).with_for_update())
        if account is None:
            account = ConnectedAccount(platform=platform, external_account_id=external_id)
            db.add(account)
        account.display_name = (str(name or external_id))[:500]
        oauth.store_credentials(account, credentials)
        db.commit()
    except (publishing.PublishingUnavailable, IntegrityError):
        db.rollback()
        return redirect(platform, False)
    return redirect(platform, True)


@router.get("/accounts")
def accounts(db: Session = Depends(session), _: str = Depends(require_auth)) -> list[dict]:
    rows = db.scalars(select(ConnectedAccount).order_by(ConnectedAccount.id))
    return [{
        "id": row.id, "platform": row.platform, "external_account_id": row.external_account_id,
        "display_name": row.display_name, "scopes": row.scopes, "status": row.status,
        "expires_at": row.expires_at, "daily_upload_limit": row.daily_upload_limit,
        "created_at": row.created_at, "updated_at": row.updated_at,
    } for row in rows]


@router.delete("/accounts/{account_id}", status_code=204)
async def disconnect(account_id: int, db: Session = Depends(session), _: str = Depends(require_auth)):
    account = db.get(ConnectedAccount, account_id)
    if account is None:
        raise HTTPException(404, "Connected account not found")
    credentials = ConnectedAccount(platform=account.platform, access_token=account.access_token, refresh_token=account.refresh_token)
    oauth.mark_revoked(account)
    db.commit()
    try:
        async with publishing.provider_client() as client:
            await oauth.revoke(client, credentials)
    except (publishing.PublishingUnavailable, InvalidToken, ValueError):
        pass
    return Response(status_code=204)
