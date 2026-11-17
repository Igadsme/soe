# Production readiness — Phase B

**Not production-ready for public launch.** The repository now has a hardened single-operator control plane. The owner must provision and validate the services and complete the product work below before launch. No real credentials are included.

## Done

- [x] Every application endpoint except login, `/health`, and `/ready` requires a session. Interactive API docs/OpenAPI routes are disabled. Development bypass requires explicit SOE_DEV_AUTH_BYPASS=true + development + loopback client + no password or hash; demo pipeline is development-only.
- [x] Processing jobs use configurable `max(floor, k*duration)` deadlines, hard-kill timed media subprocesses, stop lease renewal at the deadline, and record/queue or terminal-fail the result.
- [x] Source reruns are refused with 409 when any clip has a publication; publications use a clip foreign key and superseded unreferenced run folders are cleaned safely.
- [x] Migrations align missing source FKs and nullability; fresh SQLite upgrade and `alembic check` report no pending operations.
- [x] CI runs Postgres/Redis services, Alembic upgrade/check, 85% backend coverage, Ruff/pip-audit, frontend audit/typecheck/test/build, and production Compose builds.
- [x] HMAC sessions contain random IDs backed by `auth_sessions` (new migration `0003_auth_sessions`). Logout revokes server-side; expiry and revocation are checked on every request. Previous stateless tokens are invalidated by the format change.
- [x] HttpOnly session cookies, Secure in production/staging, SameSite=lax, Path=/; logout clears matching attributes. `/api/v1/auth/me` reports the current operator.
- [x] All cookie-auth mutations except login require session-bound HMAC in the `soe_csrf` cookie and `X-CSRF-Token`. Frontend requests include credentials and the CSRF header; sign-in/out controls are available.
- [x] Shared atomic Redis rate limits (60-second fixed windows, required in production), with bounded in-memory development fallback; login has a 5/minute username limit with exponential backoff independent of IP and a 20/minute IP limit. Forwarded IPs require an explicitly trusted peer CIDR. Server-level forwarded rewriting is disabled in the production entrypoint.
- [x] Media paths resolve inside `SOE_MEDIA_ROOT`, including a check at processing time; title/query/platform/ID validation; randomized upload names, MIME/signature checks and size limit. Signature checks are basic, not a full malware/media sandbox.
- [x] Security headers including production HSTS; explicit credentialed CORS allowlist. Generic failures with request IDs, JSON access logging without query strings/cookies; HTTP-client URL logging disabled. Secret settings use `SecretStr`.
- [x] Liveness does not touch the DB. Readiness checks DB connectivity, Alembic head, and configured Redis. SQLAlchemy uses **synchronous** sessions.
- [x] Backup/restore scripts and [runbook](BACKUP_RESTORE.md); Ruff, coverage tooling, dependency audits, exact frontend dependency versions, TypeScript checks, and a proposed [CI workflow](infra/ci/ci.yml). Existing `.github/workflows` were not modified.

- [x] DB-backed asynchronous source processing and job polling; fenced, renewable worker leases and retries; hourly session cleanup. Source responses hide absolute media paths. Redis uses redis.asyncio, a 200ms deadline and a three-failure/10-second circuit breaker (general requests fail open, login fails closed). Tests isolate dotenv and database settings and enforce 85% backend coverage.

- [x] Sentinel: synchronous DB routes execute in the threadpool; worker claim sweeps expired leases into retry/failed and updates workflow state. Active-source partial unique index and SKIP LOCKED claims prevent duplicate work. Clip writes replace prior source clips atomically on success; failed/stale attempts remove their rendered files. Paths include source, workflow run, and lease owner.
- [x] Pure ASGI body guard authenticates uploads and validates CSRF before reading any bytes; checks declared and streamed size. Other mutation bodies are capped at 64 KiB. Upload extensions and MP4/MOV brands or WebM DocType are checked; all failed uploads remove partial files.
- [x] Rights require 20 non-whitespace evidence characters and explicit known publication platforms; empty scope denies ingestion/publication. Owned rights require uploads. Naive expiry is normalized to UTC. Duplicate external IDs return 409; provider failures return generic 502/503.
- [x] FFmpeg/ffprobe have subprocess timeouts; ingest caps media size and duration. Whisper accepts the media file path instead of an in-memory PCM waveform; model cache uses a configurable shared named volume.
- [x] Production/staging reject plaintext admin passwords and publishing enabled. Unknown environments are rejected; defaults are production and demo off. Development Compose provides explicit development-only login credentials (`operator` / `development-only-password`); bypass remains loopback-only.
- [x] Migration 0005 adds publication uniqueness and session fingerprint/idle metadata; it invalidates existing sessions and collapses duplicate publication rows to the oldest ID while preserving metric references. Publish requests return the existing (clip, platform) publication.
- [x] Logout-all, credential fingerprint checks, 30-minute idle timeout with 60-second last-seen write throttle, idle session pruning, session-bound CSRF, PBKDF2 600,000 iterations with stored count, and shared full-hour SRT formatting are implemented.

Legacy `salt$hash` passwords still verify at their original 200,000 iterations and log a warning. Environment-provided hashes cannot be transparently rewritten: regenerate `SOE_ADMIN_PASSWORD_HASH` with the command below to get `pbkdf2_sha256$600000$salt$hash`, deploy it to every replica, and restart. Credential changes invalidate all sessions on the updated replicas.

Publishing remains blocked: **publishing.py's YouTube upload sends metadata only, with no media upload**; **the Instagram adapter puts the access token in the query string**. Implement resumable media upload and move Instagram authentication to a safe authorization header before enabling either adapter. Production/staging refuse the feature flag until that implementation is reviewed.

## Remaining (owner actions)

### 1. Create credentials and configure the environment

1. Install the backend requirements in an isolated environment. Copy `.env.example` to `.env`; restrict permissions with `chmod 600 .env`. Never commit `.env`, credential files, dumps, or generated values.
2. Generate a signing key locally: `python -c 'import secrets; print(secrets.token_urlsafe(48))'`. Save the output directly to a secret manager as `SOE_SESSION_SECRET`, then inject it at deployment. Rotating it invalidates all sessions.
3. Generate a PBKDF2 password hash without putting the password in shell history:

   ```bash
   PYTHONPATH=backend python -c 'from getpass import getpass; from app.auth import make_password_hash; print(make_password_hash(getpass("New admin password: ")))'
   ```

   Store it as `SOE_ADMIN_PASSWORD_HASH`; if using dotenv, single-quote the full value to preserve `$`. Set `SOE_ADMIN_USERNAME`; leave `SOE_ADMIN_PASSWORD` empty. Plaintext password verification is development-only. Use a strong unique password; MFA and per-user roles remain future work.
4. Set `SOE_ENV=production`, `SOE_DEMO_MODE=false`, `SOE_PUBLISHING_ENABLED=false`, the database URL, media root, and exact `SOE_CORS_ORIGINS`. Set `NEXT_PUBLIC_API_URL` before building the frontend (it is public, build-time configuration, never a secret).
5. Use one browser origin for frontend and API (proxy `/api` to the API and set `NEXT_PUBLIC_API_URL` to that origin). This lets frontend JavaScript read the host-only CSRF cookie. Separate frontend/API hostnames need a same-origin proxy design before use; CORS alone does not make an API host's CSRF cookie readable. SameSite=lax also requires same-site deployment.

### 2. Enable YouTube discovery

1. In Google Cloud Console, create/select an owned project; enable **YouTube Data API v3** under APIs & Services → Library.
2. Under Credentials, create an API key. Restrict it to the YouTube Data API and, where available, the backend's fixed egress IPs. Set quota alerts and a spending/usage policy.
3. Store it in the secret manager as `SOE_YOUTUBE_API_KEY`; inject only into the backend, restart, then test a small authorized metadata search with demo mode disabled. Metadata access grants no reuse rights. See Google's [credential instructions](https://developers.google.com/youtube/registering_an_application).

### 3. Enable Twitch discovery

1. Enable two-factor authentication on the owning Twitch account. Open the [Twitch developer console registration flow](https://dev.twitch.tv/docs/authentication/register-app/), register an application, and provide the required application details and owned HTTPS redirect URL.
2. In Manage, obtain the client ID and generate a client secret. Store as `SOE_TWITCH_CLIENT_ID` and `SOE_TWITCH_CLIENT_SECRET`; inject into the backend and restart. Rotating the secret invalidates the old one.
3. Test an authenticated channel search with demo mode disabled and monitor provider limits. The current adapter obtains an app access token via client credentials; this does not authorize publishing or reusing another creator's media.

### 4. YouTube OAuth publishing — not implemented end to end

1. In the Google project, configure the OAuth consent screen, support contact, authorized domains, privacy policy, and test users.
2. Create a Web application OAuth client with the exact HTTPS callback URI **after implementing a callback endpoint**. There is currently no supported callback, account connection flow, encrypted refresh-token store, refresh handling, or complete media upload worker.
3. Implement authorization-code exchange with state validation, encrypted token storage/rotation, least-privilege `youtube.upload` scope, refresh/revocation, resumable media upload, retries and idempotency. Existing publisher classes are incomplete boundaries, not working account integration.
4. Complete applicable Google verification/YouTube API compliance review and test private uploads on an authorized account before enabling publishing. Follow Google's [authorization guide](https://developers.google.com/youtube/data/guides/authentication). Do not treat an API key as upload authorization.

### 5. Choose hosting and HTTPS ingress

1. Choose a VM/container platform or managed container service, region, budget and operator. Configure DNS and a TLS-terminating reverse proxy (for example Caddy/Nginx or a managed load balancer); keep DB/Redis private.
2. Route browser traffic through one HTTPS origin; proxy the API paths and health checks, preserve `X-Request-ID`, replace untrusted incoming forwarded headers, and set request size/time limits suitable for uploads. Keep Compose ports bound to loopback behind the proxy.
3. Configure `SOE_TRUSTED_PROXIES` with only actual proxy peer IPs/CIDRs (never `0.0.0.0/0`). For custom Uvicorn launches disable its automatic proxy-header rewriting so the application can validate the real peer.
4. Run migrations once before starting replicas: `docker compose -f docker-compose.prod.yml run --rm migrate`. Start services, verify `/health`, `/ready`, authentication, CSRF, secure cookies and a real browser upload. Load-test inference separately. Docker builds/hosting need validation on the selected platform.

### 6. Database, backups and media storage

1. Choose managed PostgreSQL with TLS, private networking, least-privilege credentials, automated backups and point-in-time recovery. Set `SOE_DATABASE_URL` to the psycopg connection URL; URL-encode password characters. The bundled `db` service is a single-host alternative, not managed HA.
2. Set recovery point/time objectives and backup retention; schedule and monitor backups, encrypt them, copy off-site, restrict deletion access, and perform restore drills using [BACKUP_RESTORE.md](BACKUP_RESTORE.md). The scripts target the Compose `db`; use provider-native tooling for a managed database.
3. Provision private object storage with encryption, lifecycle/retention rules and restricted IAM. Implement an object-storage adapter plus signed downloads and ingestion into trusted local workspaces; the application currently uses a local/shared volume, not object storage. Back up media separately from PostgreSQL.
4. The worker deletes expired/revoked `auth_sessions` hourly after `SOE_SESSION_RETENTION_SECONDS` (default seven days). Approve the retention period and monitor worker health. Signing keys, database credentials, and backup keys need rotation/recovery procedures.

### 7. Secret management, scaling and monitoring

1. Select a secret manager (cloud-native or Vault), restrict deployment identity access, inject values at runtime, and configure rotation. Do not put secrets in frontend variables, image layers or logs. Configure log collection to omit query strings and credentials at the proxy/hosting layer too.
2. Monitor Redis availability: general rate limiting fails open with a sanitized warning; login fails closed with 503 “login temporarily unavailable” and Retry-After: 60. Keep all API replicas on the same Redis database (`SOE_REDIS_URL`). Redis restarts/data loss clear counters.
3. Source processing now uses durable DB jobs (migration `0004_jobs`), active-source deduplication, hashed idempotency keys, transactional claims, renewable leases, retry/backoff and terminal failed-job inspection. API requests enqueue and return 202; authenticated job endpoints support polling. Workers may scale against shared PostgreSQL/media storage. Validate capacity, monitor queue age/failures and define workspace retention. Publication execution remains incomplete.
4. Configure centralized JSON log retention/search and request-ID correlation, service/database/queue metrics, uptime/readiness alerts, disk/upload capacity alerts, provider quota alerts and error tracking with secret/PII scrubbing. Current error logs intentionally omit raw exceptions; add sanitized diagnostics before operational use.
5. Complete security review, adversarial media testing/sandboxing, abuse controls, rights/legal review, retention/deletion policy, browser E2E and disaster recovery/load drills. Single-operator auth does not provide tenant isolation or user roles.

### 8. Apply CI and resolve the second scaffold

1. Review `infra/ci/ci.yml`, set repository protections and dependency-update ownership, and have a user/token with **workflow permission** copy it to `.github/workflows/ci.yml`. This task deliberately does not change workflow files. CI includes lint, coverage report, both audits, typecheck/build, a fresh SQLite migration, Compose config and production image builds using placeholders.
2. Run `scripts/ci_checks.sh` in the configured Python environment locally. The backend line coverage threshold is enforced at 85%; do not confuse test coverage with a launch approval.
3. Decide the fate of `src/soe` and `tests/test_app.py`: they are a **separate unused FastAPI scaffold**, not the production `backend/app` application. Recommendation: delete them in a separate owner-approved cleanup after checking for unique behavior and porting any needed tests. Deletion removes ambiguous entrypoints and maintenance overhead. Merging useful parts into `backend/app` preserves behavior but needs explicit compatibility review; retaining a separate package requires its own ownership, release process and tests and keeps deployment ambiguity. Deletion is preferred because this scaffold is unused. They are intentionally unchanged in this phase and still included in verification.


## Container-host readiness additions

### Done

- Caddy same-origin HTTPS ingress, public metrics denial, shared upload cap, security headers, compressed responses, sanitized JSON access logs and persistent certificate/config volumes. Explicit subnet and trusted Caddy address; loopback ports require the debugging override.
- SHA-tagged build/migrate/start/HTTPS-check deploy script and explicit previous-ref rollback wrapper; migration downgrade risks documented.
- Opt-in scheduled PostgreSQL custom archives, validation, local retention, optional client encryption and S3-compatible upload. Failure exits nonzero; local manual scripts retained and relative restore paths fixed.
- Opt-in protected Prometheus HTTP/queue metrics with Gunicorn multiprocess lifecycle, plus opt-in scrubbed Sentry for API and worker. Unit tests added.
- Runbooks cover DNS/first release, migrations, rollback, off-site recovery, rotation, scaling, alerts and incidents. Fly/Render files are owner-review templates only.
- `.dockerignore` excludes `src/`, `tests/test_app.py` and backup artifacts. API Dockerfile copies only `backend/app` application code; Compose/platform entrypoints use `app.main:app`, never the scaffold. Protected scaffold files remain unchanged.

### Owner actions before launch

Choose and secure a host, check subnet availability, provision DNS/storage/credentials, build and validate images on that host, issue real TLS certificates and perform browser/processing smoke tests. Enable scheduled off-site encrypted backups, remote lifecycle/deletion protection and host-level failure/staleness alerting; run a timed restore from a downloaded remote object. Back up media separately. Establish RPO/RTO, retention and incident ownership. Configure private authenticated metrics scraping, external HTTPS checks, log collection and optional Sentry privacy review. Validate scaling/storage capacity and review optional platform templates before any provisioning. Existing provider publishing, security/legal and product launch blockers above still apply; infrastructure additions do not make publishing complete. No cloud accounts/resources were created.


### Repository verification (2026-11-17)

- Required dependency installation and full pytest command: **130 passed, 3 skipped; 97.77% backend coverage** (85% threshold unchanged).
- `ruff check backend/app`: passed. `pip-audit -r backend/requirements.txt`: no known vulnerabilities.
- Production Compose and local-ports override `config --quiet` using `/tmp/soe-test.env`: passed without requiring a real `.env`.
- `bash -n scripts/*.sh`, scheduled-backup shell syntax and Caddy `caddy:2-alpine` validation for localhost: passed.
- Backup image built successfully. No Compose services were started, no existing containers/volumes removed, and no cloud resources created. Actual public TLS issuance, off-site transfer, restore drills and platform deployment remain owner validation tasks.
