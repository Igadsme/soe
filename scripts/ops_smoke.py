"""Production-like smoke test through the TLS reverse proxy.

Usage:
  python scripts/ops_smoke.py --base https://localhost --ca root.crt --password-file PATH --media FILE --out result.json

Exercises health/readiness, proxy security headers, login + CSRF, upload, rights gating, real processing by
the worker, QC, and a ranged inline clip download. Prints no secrets.
"""
import argparse
import http.cookiejar
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

TERMINAL = {"succeeded", "failed", "dead_letter", "cancelled"}


class Client:
    def __init__(self, base: str, ca: str | None):
        context = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
        self.base, self.jar = base, http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context),
                                                  urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None, headers=None, content_type=None):
        request_headers = dict(headers or {})
        csrf = next((cookie.value for cookie in self.jar if cookie.name == "soe_csrf"), None)
        if csrf and method != "GET":
            request_headers["X-CSRF-Token"] = urllib.parse.unquote(csrf)
        if isinstance(body, dict):
            body, content_type = json.dumps(body).encode(), "application/json"
        if content_type:
            request_headers["Content-Type"] = content_type
        request = urllib.request.Request(self.base + path, data=body, method=method, headers=request_headers)
        try:
            with self.opener.open(request, timeout=600) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--ca")
    parser.add_argument("--user", default="operator")
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--media", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    client, checks = Client(args.base, args.ca), []

    def check(name, ok, detail=""):
        checks.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": detail})
        print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
        return ok

    status, headers, _ = client.call("GET", "/health")
    check("health via TLS proxy", status == 200, f"HTTP {status}")
    check("HSTS header", "max-age" in (headers.get("Strict-Transport-Security") or ""))
    check("nosniff + frame deny", headers.get("X-Content-Type-Options") == "nosniff" and headers.get("X-Frame-Options") == "DENY")
    check("server header removed", headers.get("Server") is None)
    status, _, body = client.call("GET", "/ready")
    check("readiness (db, redis, migrations at head)", status == 200, body.decode(errors="replace")[:200])
    status, _, _ = client.call("GET", "/metrics")
    check("metrics not exposed publicly", status == 404, f"HTTP {status}")
    status, _, _ = client.call("GET", "/api/v1/sources")
    check("API requires auth", status == 401, f"HTTP {status}")
    status, _, _ = client.call("GET", "/login")
    check("frontend served through proxy", status == 200, f"HTTP {status}")

    form = urllib.parse.urlencode({"username": args.user, "password": Path(args.password_file).read_text().strip()}).encode()
    status, _, _ = client.call("POST", "/api/v1/auth/login", form, content_type="application/x-www-form-urlencoded")
    if not check("login with password hash", status == 200, f"HTTP {status}"):
        return finish(args.out, checks)
    check("session cookies are Secure", all(cookie.secure for cookie in client.jar if cookie.name in {"soe_session", "soe_csrf"}))

    media = Path(args.media)
    boundary = uuid.uuid4().hex
    payload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\nOps smoke {media.name}\r\n"
               f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{media.name}\"\r\n"
               "Content-Type: video/mp4\r\n\r\n").encode() + media.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    started = time.monotonic()
    status, _, body = client.call("POST", "/api/v1/sources/upload", payload, content_type=f"multipart/form-data; boundary={boundary}")
    if not check("upload through proxy", status in (200, 201), f"HTTP {status}, {media.stat().st_size} bytes"):
        return finish(args.out, checks)
    source_id = json.loads(body)["source_id"]
    status, _, _ = client.call("POST", f"/api/v1/sources/{source_id}/process")
    check("processing refused without rights", status in (403, 404, 409), f"HTTP {status}")
    rights = {"authorization_type": "owned", "evidence": "Original synthetic media generated for ops verification.",
              "allowed_platforms": ["youtube"], "commercial_use": True, "publishing_allowed": True}
    status, _, _ = client.call("POST", f"/api/v1/sources/{source_id}/rights", rights)
    check("rights recorded", status == 201, f"HTTP {status}")
    status, _, body = client.call("POST", f"/api/v1/sources/{source_id}/process")
    if not check("processing queued", status == 202, f"HTTP {status}"):
        return finish(args.out, checks)
    job_id, job = json.loads(body)["job_id"], {}
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        job = json.loads(client.call("GET", f"/api/v1/jobs/{job_id}")[2])
        if job.get("status") in TERMINAL:
            break
        time.sleep(3)
    check("worker processed real media", job.get("status") == "succeeded",
          f"status={job.get('status')} result={job.get('result')} after {time.monotonic() - started:.1f}s")
    clips = []
    while time.monotonic() < deadline:
        clips = [clip for clip in json.loads(client.call("GET", "/api/v1/clips")[2]) if clip["source_id"] == source_id]
        if clips and all(clip["status"] not in {"qc_pending", "rendering"} for clip in clips):
            break
        time.sleep(3)
    check("QC completed for every clip", bool(clips) and all(clip["status"] not in {"qc_pending", "rendering"} for clip in clips),
          ", ".join(f"#{clip['id']}:{clip['status']}" for clip in clips))
    if clips:
        status, headers, body = client.call("GET", f"/api/v1/clips/{clips[0]['id']}/download?inline=true", headers={"Range": "bytes=0-1023"})
        check("ranged inline clip download", status == 206 and len(body) == 1024 and "inline" in (headers.get("Content-Disposition") or ""),
              f"HTTP {status}, {len(body)} bytes, {headers.get('Content-Range')}")
    return finish(args.out, checks, {"source_id": source_id, "job_id": job_id, "clips": len(clips)})


def finish(out, checks, extra=None) -> int:
    failed = sum(item["result"] == "FAIL" for item in checks)
    Path(out).write_text(json.dumps({"checks": checks, "passed": len(checks) - failed, "failed": failed, **(extra or {})}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
