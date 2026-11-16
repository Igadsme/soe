# Production readiness — Phase B

**Not production-ready for public launch.** The repository now has a hardened single-operator control plane. The owner must provision and validate the services and complete the product work below before launch. No real credentials are included.

## Done

- [x] Every application endpoint except login, `/health`, and `/ready` requires a session. Interactive API docs/OpenAPI routes are disabled. Development bypass requires development + demo mode + no password or hash; demo pipeline is development-only.
- [x] HMAC sessions contain random IDs backed by `auth_sessions` (new migration `0003_auth_sessions`). Logout revokes server-side; expiry and revocation are checked on every request. Previous stateless tokens are invalidated by the format change.
- [x] HttpOnly session cookies, Secure in production, SameSite=lax, Path=/; logout clears matching attributes. `/api/v1/auth/me` reports the current operator.
- [x] All cookie-auth mutations except login require matching `soe_csrf` cookie and `X-CSRF-Token`. Frontend requests include credentials and the CSRF header; sign-in/out controls are available.
- [x] Shared atomic Redis rate limits (60-second fixed windows, required in production), with bounded in-memory development fallback; login has a 5/minute IP+username limit and a 20/minute IP limit. Forwarded IPs require an explicitly trusted peer CIDR. Server-level forwarded rewriting is disabled in the production entrypoint.
- [x] Media paths resolve inside `SOE_MEDIA_ROOT`, including a check at processing time; title/query/platform/ID validation; randomized upload names, MIME/signature checks and size limit. Signature checks are basic, not a full malware/media sandbox.
- [x] Security headers including production HSTS; explicit credentialed CORS allowlist. Generic failures with request IDs, JSON access logging without query strings/cookies; HTTP-client URL logging disabled. Secret settings use `SecretStr`.
- [x] Liveness does not touch the DB. Readiness checks DB connectivity and Alembic head. SQLAlchemy uses **synchronous** sessions.
- [x] Backup/restore scripts and [runbook](BACKUP_RESTORE.md); Ruff, coverage tooling, dependency audits, exact frontend dependency versions, TypeScript checks, and a proposed [CI workflow](infra/ci/ci.yml). Existing `.github/workflows` were not modified.

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
4. Schedule deletion of expired/revoked `auth_sessions` according to an approved audit retention period. Signing keys, database credentials, and backup keys need rotation/recovery procedures.

### 7. Secret management, scaling and monitoring

1. Select a secret manager (cloud-native or Vault), restrict deployment identity access, inject values at runtime, and configure rotation. Do not put secrets in frontend variables, image layers or logs. Configure log collection to omit query strings and credentials at the proxy/hosting layer too.
2. Monitor Redis availability: general rate limiting fails open with a sanitized warning; login fails closed with 503 “login temporarily unavailable” and Retry-After: 60. Keep all API replicas on the same Redis database (`SOE_REDIS_URL`). Redis restarts/data loss clear counters.
3. Choose a durable queue, implement locking, retry/backoff, idempotency and dead-letter handling for media/publication jobs. The current sync processing endpoint and small worker are not a production queue.
4. Configure centralized JSON log retention/search and request-ID correlation, service/database/queue metrics, uptime/readiness alerts, disk/upload capacity alerts, provider quota alerts and error tracking with secret/PII scrubbing. Current error logs intentionally omit raw exceptions; add sanitized diagnostics before operational use.
5. Complete security review, adversarial media testing/sandboxing, abuse controls, rights/legal review, retention/deletion policy, browser E2E and disaster recovery/load drills. Single-operator auth does not provide tenant isolation or user roles.

### 8. Apply CI and resolve the second scaffold

1. Review `infra/ci/ci.yml`, set repository protections and dependency-update ownership, and have a user/token with **workflow permission** copy it to `.github/workflows/ci.yml`. This task deliberately does not change workflow files. CI includes lint, coverage report, both audits, typecheck/build, a fresh SQLite migration, Compose config and production image builds using placeholders.
2. Run `scripts/ci_checks.sh` in the configured Python environment locally. Choose a meaningful coverage threshold after measuring the current baseline; do not confuse test coverage with a launch approval.
3. Decide the fate of `src/soe` and `tests/test_app.py`: they are a **separate unused FastAPI scaffold**, not the production `backend/app` application. Recommendation: remove them in an owner-approved cleanup, or move any useful behavior into `backend/app` and consolidate tests. They are intentionally unchanged in this phase and still included in verification.
