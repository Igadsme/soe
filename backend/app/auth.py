import base64
import hmac
import logging
import secrets
import time
from hashlib import pbkdf2_hmac
from ipaddress import ip_address

from fastapi import HTTPException, Request

from .config import secret_value, settings
from .db import SessionLocal
from .models import AuthSession


def _key() -> bytes:
    if not settings.session_secret:
        raise HTTPException(503, "SOE_SESSION_SECRET is not configured")
    return secret_value(settings.session_secret).encode()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(payload_b64: str) -> str:
    return hmac.new(_key(), payload_b64.encode("ascii"), "sha256").hexdigest()


def make_session(username: str) -> str:
    """Return a signed session token: ``<base64url(payload)>.<hex(hmac-sha256)>``.

    Neither part can contain ``.``, so the token always splits unambiguously.
    """
    expires = int(time.time()) + settings.session_ttl_seconds
    sid = secrets.token_hex(32)
    with SessionLocal() as db:
        db.add(AuthSession(id=sid, username=username, expires_at=expires, last_seen_at=int(time.time()), password_fingerprint=password_fingerprint()))
        db.commit()
    payload_b64 = _b64encode(f"{username}:{sid}:{expires}".encode())
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_session(value: str | None) -> str:
    if not value:
        raise HTTPException(401, "authentication required")
    try:
        payload_b64, signature = value.split(".")
        if not hmac.compare_digest(signature.encode("ascii"), _sign(payload_b64).encode("ascii")):
            raise ValueError
        username, sid, expiry = _b64decode(payload_b64).decode().rsplit(":", 2)
        if int(expiry) < int(time.time()):
            raise ValueError
        with SessionLocal() as db:
            row = db.get(AuthSession, sid)
            if not row or row.revoked_at is not None or row.expires_at <= int(time.time()) or row.username != username or row.password_fingerprint != password_fingerprint() or row.last_seen_at + settings.session_idle_seconds <= int(time.time()):
                raise ValueError
            if int(time.time()) - row.last_seen_at >= 60:
                row.last_seen_at = int(time.time())
                db.commit()
        return username
    except (ValueError, UnicodeError, TypeError):
        raise HTTPException(401, "invalid or expired session") from None


def hash_password(password: str, salt: bytes) -> str:
    return pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()


def make_password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = pbkdf2_hmac("sha256", password.encode(), salt, 600_000).hex()
    return f"pbkdf2_sha256$600000${salt.hex()}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        if stored.startswith("pbkdf2_sha256$"):
            _, iterations, salt, digest = stored.split("$")
            return hmac.compare_digest(pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex(), digest)
        salt, digest = stored.split("$")
        logging.getLogger(__name__).warning("Legacy password hash; regenerate SOE_ADMIN_PASSWORD_HASH with make_password_hash")
        return hmac.compare_digest(hash_password(password, bytes.fromhex(salt)), digest)
    except (ValueError, TypeError, OverflowError):
        return False


def password_fingerprint() -> str:
    return hmac.new(_key(), (secret_value(settings.admin_password_hash) + secret_value(settings.admin_password)).encode(), "sha256").hexdigest()


def csrf_token(session: str) -> str:
    sid = _b64decode(session.split(".")[0]).decode().rsplit(":", 2)[1]
    return _sign(sid)


def development_bypass(request: Request | None = None) -> bool:
    try:
        loopback = request is not None and request.client is not None and ip_address(request.client.host).is_loopback
    except ValueError:
        loopback = False
    return settings.dev_auth_bypass and settings.env == "development" and loopback and not settings.admin_password and not settings.admin_password_hash


def revoke_session(value: str) -> None:
    sid = _b64decode(value.split(".")[0]).decode().rsplit(":", 2)[1]
    with SessionLocal() as db:
        row = db.get(AuthSession, sid)
        if row:
            row.revoked_at = int(time.time())
            db.commit()


def require_auth(request: Request) -> str:
    if development_bypass(request):
        return "development-operator"
    username = verify_session(request.cookies.get("soe_session"))
    if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
        cookie = request.cookies.get("soe_csrf", "")
        header = request.headers.get("X-CSRF-Token", "")
        if not cookie or not hmac.compare_digest(csrf_token(request.cookies["soe_session"]).encode(), header.encode()) or not hmac.compare_digest(cookie.encode(), header.encode()):
            raise HTTPException(403, "CSRF validation failed")
    return username
