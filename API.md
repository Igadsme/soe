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
- `POST /api/v1/clips/{id}/publish?platform=...` — rights-gated queue operation; disabled unless `SOE_PUBLISHING_ENABLED=true`.
- `GET /api/v1/analytics` — separate verified and unverified metric observations.
- `POST /api/v1/auth/login` / `POST /api/v1/auth/logout` — signed HttpOnly session lifecycle; development mode is explicitly bypassed only when no admin password is configured.
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
