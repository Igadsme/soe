import base64
from hashlib import pbkdf2_hmac
import hmac
import secrets
import time
from fastapi import HTTPException, Request
from .config import settings


def _key() -> bytes:
    if not settings.session_secret:
        raise HTTPException(503, "SOE_SESSION_SECRET is not configured")
    return settings.session_secret.encode()


def make_session(username: str) -> str:
    expires = int(time.time()) + settings.session_ttl_seconds
    payload = f"{username}:{expires}".encode()
    signature = hmac.new(_key(), payload, "sha256").digest()
    return base64.urlsafe_b64encode(payload + b"." + signature).decode()


def verify_session(value: str | None) -> str:
    if not value:
        raise HTTPException(401, "authentication required")
    try:
        decoded = base64.urlsafe_b64decode(value.encode())
        payload, signature = decoded.rsplit(b".", 1)
        if not hmac.compare_digest(signature, hmac.new(_key(), payload, "sha256").digest()):
            raise ValueError
        username, expiry = payload.decode().rsplit(":", 1)
        if int(expiry) < int(time.time()):
            raise ValueError
        return username
    except (ValueError, UnicodeDecodeError, TypeError):
        raise HTTPException(401, "invalid or expired session")


def hash_password(password: str, salt: bytes) -> str:
    return pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()


def make_password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"{salt.hex()}${hash_password(password, salt)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        return hmac.compare_digest(hash_password(password, salt), digest)
    except (ValueError, TypeError):
        return False


def require_auth(request: Request) -> str:
    if settings.demo_mode and settings.env == "development" and not settings.admin_password:
        return "development-operator"
    return verify_session(request.cookies.get("soe_session"))
