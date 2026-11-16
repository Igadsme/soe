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
    payload_b64 = _b64encode(f"{username}:{expires}".encode())
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_session(value: str | None) -> str:
    if not value:
        raise HTTPException(401, "authentication required")
    try:
        payload_b64, signature = value.split(".")
        if not hmac.compare_digest(signature.encode("ascii"), _sign(payload_b64).encode("ascii")):
            raise ValueError
        username, expiry = _b64decode(payload_b64).decode().rsplit(":", 1)
        if int(expiry) < int(time.time()):
            raise ValueError
        return username
    except (ValueError, UnicodeError, TypeError):
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
