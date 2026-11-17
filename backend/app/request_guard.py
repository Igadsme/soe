"""Enforce authentication and byte budgets before multipart parsing."""
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .auth import require_auth
from .config import settings


class BodyTooLarge(Exception):
    pass


class RequestGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH", "DELETE"}:
            return await self.app(scope, receive, send)
        request = Request(scope)
        upload = scope["path"].rstrip("/") == "/api/v1/sources/upload"
        limit = settings.max_upload_bytes if upload else 65536
        try:
            if upload:
                await run_in_threadpool(require_auth, request)
            length = request.headers.get("content-length")
            if length is not None and (int(length) < 0 or int(length) > limit):
                raise BodyTooLarge
        except HTTPException as exc:
            return await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)
        except (ValueError, BodyTooLarge):
            return await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)
        # Spool within the budget before handing a body to parsers, so their error
        # handlers cannot swallow an overflow or leave multipart temporary files.
        from tempfile import SpooledTemporaryFile
        with SpooledTemporaryFile(max_size=65536) as body:
            total = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                total += len(chunk)
                if total > limit:
                    return await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)
                await run_in_threadpool(body.write, chunk)
                if not message.get("more_body", False):
                    break
            body.seek(0)
            delivered = False
            async def bounded_receive():
                nonlocal delivered
                if delivered:
                    return await receive()
                chunk = await run_in_threadpool(body.read, 65536)
                delivered = body.tell() >= total
                return {"type": "http.request", "body": chunk, "more_body": not delivered}
            await self.app(scope, bounded_receive, send)
