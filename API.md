# API

- `GET /health` — service health.
- `GET /ready` — database readiness; returns `503` when the database is unavailable.
- `GET /api/v1/overview` — persisted counts and explicitly verified financial totals.
- `POST /api/v1/sources` — register a YouTube/Twitch metadata source.
- `GET /api/v1/sources` — list registered sources.
- `POST /api/v1/sources/{id}/rights` — attach an authorization record; fails closed for non-commercial or non-publishing permissions.
- `GET /api/v1/clips` — list ranked clip candidates.
- `POST /api/v1/demo/pipeline` — create clearly labeled local fixture records; performs no fake media processing.
- `POST /api/v1/discovery/search?platform=youtube|twitch&query=...` — use an official adapter when credentials exist, otherwise an explicitly labeled fixture in development mode.
- `GET /api/v1/publications` — list publication queue/history records.
- `POST /api/v1/clips/{id}/publish` — rights-gated publication queue for a rendered clip and connected account.
- `GET /api/v1/analytics` — separate verified and unverified metric observations.
- `POST /api/v1/auth/login` / `POST /api/v1/auth/logout` — signed HttpOnly session lifecycle; development bypass requires explicit opt-in, a loopback peer, and no configured credentials.
- `POST /api/v1/sources/upload` — validated local video upload, initially stored as `rights_review`.
- `POST /api/v1/sources/{id}/process` — rights/path-gated enqueue. Returns `202 {"job_id": "...", "status": "queued"}`; no inference runs in the request.
- `GET /api/v1/clips/{id}/download` — authenticated rendered MP4 download.
- `GET /api/v1/revenue`, `GET /api/v1/agents`, `GET /api/v1/settings` — verified ledger entries, agent capability status, and safe non-secret runtime configuration.

- `GET /api/v1/jobs/{id}` — authenticated status (`queued`, `running`, `succeeded`,
  `failed`), attempts, run_after, sanitized last_error, result and timestamps;
  returns 404 for an unknown ID.
- `GET /api/v1/jobs` — authenticated latest 100 jobs, newest first.

Process accepts an optional `Idempotency-Key` header (1–255 characters).
Repeating a request while the source has a queued/running job returns the same job
with HTTP 200. A supplied key also replays a completed job; using it for another
source returns 409. Rights and local path checks still run before replay/enqueue
(403/404/422). Poll the job endpoint until succeeded/failed, then refresh sources
and clips. Success `result` contains workflow_id, source_id, probe,
transcript_segments and clips_generated. Workers retry failures up to three
attempts with exponential delays and reclaim expired leases. A WorkflowRun is
created at enqueue and moves queued → transcribing → edited, or back to queued
for retry / failed on exhaustion. Successful replay creates no duplicate clips.
Source read responses expose `has_media`; they never expose `media_path`.

## Sentinel hardening and durable jobs

- `POST /api/v1/auth/logout-all`: authenticated, CSRF-protected; returns `200 {"authenticated": false}` and revokes every session for this operator. Existing session cookies subsequently return 401. Logout also revokes server-side. Sessions expire after 1,800 idle seconds by default; credential hash changes invalidate them. CSRF is bound to the session; send the `soe_csrf` cookie value as `X-CSRF-Token`.
- `POST /api/v1/auth/login`: form body capped at 64 KiB before parsing. Returns 401 for invalid credentials, 429 with Retry-After for per-username exponential backoff/username or IP windows, 503 if authentication or required Redis is unavailable. Production/staging cookies are Secure.
- `POST /api/v1/sources/upload`: cookie/session and CSRF checked before body reads (401/403). Declared or streamed bodies above SOE_MAX_UPLOAD_BYTES return 413. MP4/MOV require an ftyp box with a known brand; WebM requires EBML and WebM DocType. Invalid types/signatures return 415. Files use .mp4, .mov, or .webm according to validated MIME.
- Other POST/PUT/PATCH/DELETE bodies are limited to 64 KiB, returning 413 before parsing on overflow.
- `POST /api/v1/sources`: duplicate external_id returns 409.
- `POST /api/v1/sources/{id}/rights`: owned requires an upload source; evidence requires at least 20 non-whitespace characters; allowed_platforms is a required non-empty list drawn from upload/youtube/twitch/tiktok/instagram. Invalid input returns 422. Naive expires_at is interpreted as UTC. Empty existing scopes deny processing and publishing.
- `POST /api/v1/sources/{id}/process`: 202 with job_id/status for a new durable job; 200 for the same idempotency key or an existing active source job; 409 for a conflicting key. No media work runs in this request. Expired worker leases retry up to max_attempts then fail, including workflow state. Poll `GET /api/v1/jobs/{job_id}` or `GET /api/v1/jobs`.
- Discovery provider 401/403, rate limits and timeouts return generic 503; malformed provider data returns generic 502.
- `/ready`: 503 if DB, migration head, or configured Redis is unavailable; `/health` remains lightweight.

Environment defaults are production/demo off. Staging has the same startup validation, HSTS and Secure cookie policy as production. Development bypass requires explicit opt-in, a loopback peer and no configured credentials.

## Connected accounts and publishing

- `GET /api/v1/oauth/{platform}/start` returns an `authorization_url` for the signed-in operator. Navigate to it in that browser. Returns 409 if the client, redirect origin, or encryption key is missing. Accepted platforms are `youtube`, `tiktok`, and `instagram`.
- `GET /api/v1/oauth/{platform}/callback` checks the signed-in operator’s browser session and atomically consumes an unexpired state for that platform. It redirects to Settings with `oauth=success` or `oauth=failure`. State expires after 10 minutes. YouTube also uses PKCE S256.
- `GET /api/v1/accounts` lists account IDs, platform identities, scopes, connection status, expiry, and daily upload limit. `DELETE /api/v1/accounts/{id}` clears local credentials immediately and attempts provider revocation. Mutations require the session CSRF header.

Send JSON to `POST /api/v1/clips/{id}/publish` with `platform`, `account_id`, and optional `title`, `description`, `privacy_status`, and ISO 8601 `scheduled_at`. Metadata defaults to the clip’s hook and description. Naive timestamps are UTC. The response contains `publication_id` and `status`. An identical clip, platform, account, and rendered file SHA-256 returns the existing row. A gate or account conflict returns 409, rights denial returns 403, and invalid metadata or missing rendered media returns 422.

- YouTube accepts `private` (default) and `unlisted`. Scheduled jobs start at or after the requested time and preserve their chosen visibility.
- TikTok defaults to `SELF_ONLY`. Audited apps may explicitly submit `tiktok_privacy_level` when supported by the creator’s settings and approval. `unlisted` applies only to YouTube.
- Instagram Reels use the linked professional account’s visibility. Submit `instagram_visibility_acknowledged: true` for a real upload. Captions are limited to 2,200 characters.
- `GET /api/v1/publications` reports upload progress in bytes, attempts, next attempt, sanitized errors, and provider IDs and URLs when available. `GET /api/v1/publications/{id}/events` returns timestamped state transitions and their actors.
- `POST /api/v1/publications/{id}/cancel` cancels queued or scheduled rows before upload starts. `POST /api/v1/publications/{id}/retry` requeues a dead-lettered transient or rate-limited row with its existing checkpoint. A response of 409 means the row is ineligible.
- With `SOE_PUBLISHING_DRY_RUN`, the worker performs local rights, account, quota, schedule, metadata and media checks. It records a terminal `dry_run` result without provider calls. The idempotency key still applies to that row.
