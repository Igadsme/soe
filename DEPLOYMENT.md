# Deployment runbook

This is a single-host, host-agnostic deployment. The owner supplies a Linux container host, Docker Engine with Compose v2.24+ (optional env files), Git, Python 3, curl, DNS, storage, and a secret manager. No accounts or cloud resources are created by these files. Publishing remains disabled; see PRODUCTION_READINESS.md for product launch blockers.

## First deployment

1. Choose a host with capacity for FFmpeg/Whisper and persistent storage. Allow inbound TCP 80/443 and restrict SSH. Keep PostgreSQL, Redis, API and frontend private. Point the public domain's A/AAAA records at the host; remove incorrect IPv6 records. Ensure outbound ACME, provider, package and object-store access works. The explicit Compose subnet is `172.30.0.0/24`; check for conflicts before deployment. If changing it, update Caddy's static `.2` address and `SOE_TRUSTED_PROXIES` together.
2. Clone the repository and check out an owner-reviewed tag/SHA. Copy `.env.example` to `.env`, `chmod 600 .env`, and replace credential placeholders. Set `SOE_DOMAIN` to the hostname only, `SOE_ACME_EMAIL` to an operational address, and `SOE_PUBLIC_BIND` as needed (default `0.0.0.0`). The browser API URL and credentialed CORS default to `https://${SOE_DOMAIN}`. They must be the same exact origin; the public frontend URL is compiled at build time. Keep `SOE_ENV=production`, demo and publishing false.
3. Generate a session secret locally with `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'` and store it securely as `SOE_SESSION_SECRET`. Generate the password hash interactively in an installed backend environment:

   ```sh
   PYTHONPATH=backend python -c 'from getpass import getpass; from app.auth import make_password_hash; print(make_password_hash(getpass("Admin password: ")))'
   ```

   Store the full hash single-quoted in `.env` to preserve `$`. Leave `SOE_ADMIN_PASSWORD` empty. Set strong matching `POSTGRES_PASSWORD` and URL-encoded `SOE_DATABASE_URL` credentials. The scripts and backup service target the local Compose `db`, even if the API URL points elsewhere; managed databases require separate backup tooling.
4. Build, migrate and start from the repository root:

   ```sh
   export SOE_IMAGE_TAG=$(git rev-parse HEAD)
   docker compose -f docker-compose.prod.yml config --quiet
   docker compose -f docker-compose.prod.yml build
   docker compose -f docker-compose.prod.yml run --rm migrate
   docker compose -f docker-compose.prod.yml up -d --no-deps api worker frontend caddy
   curl --fail https://app.example.com/health
   curl --fail https://app.example.com/ready
   docker compose -f docker-compose.prod.yml ps
   ```

   Use your actual domain. The one-shot migration service gates ordinary Compose API/worker startup; the explicit sequence above uses `--no-deps` after a successful migration to avoid running the migration a second time. Do not run independent migrations from each replica. Caddy requests Let's Encrypt certificates automatically, redirects HTTP to HTTPS, persists certificates/config in `caddy_data`/`caddy_config`, and proxies `/api/*`, `/health`, `/ready` to API. It returns 404 for `/metrics` and `/metrics/*`. All other routes reach frontend. The request body cap uses the same `SOE_MAX_UPLOAD_BYTES` as the API, including multipart overhead; choose a value that accommodates intended files. The API retains its smaller 64 KiB cap for other mutations.
5. Verify HTTPS, frontend sign-in, CSRF, secure cookies, logout and a small authorized upload/processing job in a browser. Confirm database/media/Whisper and certificate volumes persist across recreation. Configure and test off-site backup recovery before accepting important data.

For local TLS testing set `SOE_DOMAIN=localhost`, `NEXT_PUBLIC_API_URL=https://localhost`, `SOE_CORS_ORIGINS=https://localhost`, and rebuild frontend. Caddy automatically selects its internal CA for localhost. An explicit `tls internal` inside the site block is also available for local testing; remove it before public deployment. Export Caddy's local root certificate from `/data/caddy/pki/authorities/local/root.crt`, trust it only on test clients, and use `curl --cacert /path/root.crt https://localhost/ready`. Never disable certificate verification. Set `SOE_DEPLOY_CA_CERT` for script checks. Public certificates need reachable ports and correct DNS; validation alone does not issue a certificate.

API/frontend have no host ports by default. Temporarily opt in to loopback debugging with `docker compose -f docker-compose.prod.yml -f docker-compose.local-ports.yml up -d`. Remove that override for routine deployment. The development `docker-compose.yml` is separate.

## Routine releases and rollback

Use a clean checkout and an immutable, reviewed remote tag/SHA: `scripts/deploy.sh TAG_OR_SHA`. It fetches that ref, checks it out detached, tags builds with the resolved Git SHA, builds, runs migration once, starts services and polls HTTPS `/health` and `/ready` for up to roughly 25 minutes including curl timeouts. It prints the previous SHA as a rollback hint. A failed release exits nonzero; inspect `docker compose -f docker-compose.prod.yml logs --tail=100` and `ps`. The script does not reset dirty files or delete volumes. Record the Git SHA, migration revision, health results and backup identifier in the release record. Keep old images and source revisions until the rollback window ends.

Before subsequent manual Compose commands, set `export SOE_IMAGE_TAG=$(git rev-parse HEAD)` so image selection matches the checked-out release (or update your deployment environment's `SOE_IMAGE_TAG`). Rebuild frontend whenever the browser origin changes. The deploy script builds reproducibly named images from a Git ref; it does not accept arbitrary registry image tags. For a registry workflow, retain immutable SHA-tagged images and adapt the build step to pull those images under owner review.

`scripts/rollback.sh PREVIOUS_TAG_OR_SHA` rebuilds/redeploys that previous Git release. Database migrations are forward-only during automatic deployment: verify that the older code works with the current schema before rollback. Older Alembic code may reject a newer database revision. For incompatible changes, stop writers, take a backup, then restore an appropriate verified backup or review/test `alembic downgrade REVISION` with the migration code that created the current schema. A downgrade can lose data; never automate it blindly. Rotate session secrets after restoring old session records. See BACKUP_RESTORE.md.

## Backups and recovery

Enable the optional scheduler using `docker compose -f docker-compose.prod.yml --profile backup build backup` then `docker compose -f docker-compose.prod.yml --profile backup up -d backup`. It dumps immediately and then every `SOE_BACKUP_INTERVAL_SECONDS` (daily by default), validates the custom archive listing, optionally encrypts with GPG AES256, uploads with rclone if a bucket is configured, and only then rotates local archives older than `SOE_BACKUP_RETENTION_DAYS` (14; 0 disables deletion). The small Alpine image includes PostgreSQL 16 client, rclone and GPG. Rebuild regularly for package security fixes. Local backups default to `./backups` with restrictive file permissions.

The owner must supply an existing private S3-compatible bucket, endpoint/region, prefix and restricted access keys. Empty endpoint uses AWS S3. Set `SOE_BACKUP_S3_*` and require `SOE_BACKUP_ENCRYPTION_PASSPHRASE` for uploads (the scheduler exits before dumping if it is missing); use transport/server encryption too. Store the encryption passphrase separately from backups and test recovery. Local retention never deletes remote files; configure remote lifecycle, versioning/immutability and deletion protection separately. Back up shared media separately and coordinate DB/media recovery points.

Any dump/encryption/upload failure logs a generic error and exits nonzero without logging credentials. The service intentionally does not restart itself: alert on exit, no completed backup within 26 hours, low disk space, and missing remote objects. Investigate and restart explicitly. Retain the local recovery copy when off-site upload fails. Environment changes require recreation. Repeat the backup profile build/start commands after releases that change backup code; the application deploy script leaves this optional scheduler under explicit operator control. Manual `scripts/backup_db.sh` remains local-only, without encryption/upload. Verify both remote object presence and a full restore in an isolated database at least monthly; record measured RPO/RTO. See BACKUP_RESTORE.md for downloads, decryption and restore.

## Secrets and rotation

Inject secrets at runtime, restrict `.env` and Docker access, and keep secrets out of images, frontend variables, logs and version control. Never run `compose config` without `--quiet` into shared logs: expanded configuration contains credentials.

- Session secret: generate a fresh random value, update every API/worker and recreate them. This invalidates all active sessions; coordinate operator re-login.
- Admin password hash: generate interactively as above, update all API replicas and recreate them. Credential fingerprints invalidate sessions for changed credentials. Existing legacy hashes should be regenerated with the current iteration count.
- Database password: stop writers and scheduler, take/verify a backup, change the PostgreSQL role password through a secure database session, update both URL-encoded application URL and `POSTGRES_PASSWORD`, then recreate API/worker/backup and verify readiness. Changing initialization environment alone does not change an existing Postgres role password.
- S3 keys: issue replacement restricted keys in the owner's existing storage system, update/recreate backup, verify an uploaded object and recovery, then revoke old keys. Keep encryption recovery material through the retention period of older archives.
- Metrics token/Sentry DSN: replace runtime values, recreate API/worker, update scraper authorization or error project configuration, verify, then revoke old credentials. Empty Sentry DSN disables initialization. Do not rotate backup encryption keys without preserving keys needed by existing archives.

## Scaling and monitoring

Set `SOE_WEB_CONCURRENCY` for Gunicorn processes per API container; recreate API. `docker compose -f docker-compose.prod.yml up -d --scale worker=2` scales workers sharing PostgreSQL, Redis, media paths and Whisper cache. Durable jobs use `SKIP LOCKED`, renewable 120-second leases, ownership-fenced completion and bounded retries. Monitor queue age, failures, lease expirations, CPU/GPU/RAM and disk usage. SIGTERM drains the current job; allow enough shutdown time for media work. This fixed-address proxy topology is intended for a single host; multi-host replicas need shared media storage and reviewed ingress/networking.

Enable `SOE_METRICS_ENABLED=true` to expose Prometheus text on the API's private `/metrics` endpoint. Disabled returns 404. Production/staging require a non-empty `SOE_METRICS_TOKEN` when metrics are enabled. With a token, **all** requests require `Authorization: Bearer TOKEN`; wrong/missing tokens return 401. In development without a token, only socket peers in RFC1918, loopback, IPv6 ULA or explicitly configured `SOE_TRUSTED_PROXIES` networks are accepted (others 403); forwarded headers never grant access. Prefer a token and a scraper on the private Compose network. Caddy always blocks public metrics, including when a bearer token is supplied. Do not expose API directly to the Internet. The debugging override allows host-side token scrapes at `http://127.0.0.1:8000/metrics`.

`soe_http_requests_total` and `soe_http_request_duration_seconds` use method, route template and status labels; unmatched paths collapse to `unmatched`, keeping identifiers out of labels. `soe_http_requests_in_progress` tracks active responses. `soe_jobs{status=...}` queries queued/running/succeeded/failed totals at scrape time. DB errors make a scrape fail instead of reporting a misleading empty queue. API Compose uses a private tmpfs `PROMETHEUS_MULTIPROC_DIR`, cleaned before startup, plus Gunicorn `child_exit` cleanup for correct multi-process HTTP metrics. Each API container must have its own directory; scrape each replica separately. Custom multi-process deployments must reproduce this setup; without it metrics only reflect whichever process serves the scrape. Queue gauges are computed once per scrape, not summed per process. Metrics reset when the container restarts.

Optional Sentry initializes API and worker only when `SOE_SENTRY_DSN` is nonempty, with FastAPI integration, `send_default_pii=False`, `SOE_ENV` environment and `SOE_SENTRY_TRACES_SAMPLE_RATE` (default 0). The scrubber removes cookies, authorization/CSRF headers, request data/query strings, user data, breadcrumbs and frame locals from events/transactions. Application errors can contain domain data: review outbound events and project-side scrubbing/retention before enabling. Worker failures and sanitized API failures are explicitly captured.

Application JSON logs contain `level`, `logger`, `message`; access events add `request_id`, `method`, `path` (no query), `status`, `duration_ms`. Worker events use the same base fields. Collect stdout with service/container/time metadata supplied by the host. Caddy access logs are JSON (`ts`, `level`, `logger`, request metadata, `status`, `duration`, `size`); URI and request/response headers are removed to avoid logging credentials/query strings. Avoid debug logging in production. Correlate API request IDs; keep log access/retention restricted.

Configure an external HTTPS check every minute on both `/health` (process alive) and `/ready` (DB, migration head and Redis). Alert after three consecutive failures; warn for certificate expiry under 14 days, 5xx rate above 1% for 5 minutes, p95 API latency above 2 seconds for 10 minutes (tune to workload), queue age above 5 minutes, any sustained failed jobs, backup age over 26 hours and disk usage over 80% (critical 90%). Redis failures let general rate limits fail open and login fail closed with 503; monitor it explicitly. Health checks are not a restore drill or product correctness test.

## Incident checklist

1. Confirm scope using external checks, `ps`, sanitized logs and request IDs; record the active release SHA and timeline.
2. Stop harmful writes or affected workers when necessary. Preserve logs, backup copies and volumes; never use `down -v` as incident recovery.
3. Check disk/memory, DB/Redis reachability, migration head, queue leases, certificate/DNS and upstream quotas. Avoid exposing secrets while collecting evidence.
4. Roll back compatible application releases or restore through the tested recovery procedure; reconcile external side effects and media before restarting writers.
5. Rotate compromised credentials and invalidate sessions. Verify HTTPS/readiness, login/CSRF and a representative job; monitor until stable and document the cause and follow-up actions.

`infra/fly/` and `infra/render/` are optional owner-review templates, not validated production platforms. Review secrets, storage, ingress, metrics isolation, migration serialization, costs and provider terms before applying them. The unused `src/soe` scaffold is excluded from Docker contexts; all production commands use `app.main:app` from `backend/app`.

References: [Caddy request limits](https://caddyserver.com/docs/caddyfile/directives/request_body), [Prometheus multiprocess requirements](https://prometheus.github.io/client_python/multiprocess/), [Sentry SDK options](https://getsentry.github.io/sentry-python/api.html).

## Trusted ingress on hosted platforms

`client_ip` walks X-Forwarded-For from right to left, skipping only peers in `SOE_TRUSTED_PROXIES`; an untrusted socket peer makes spoofed forwarded headers ineffective. Empty trust ignores forwarded headers and startup warns in production/staging. Login backoff is per username/client IP, with a shared 30-attempt per username, 60-second window across IPs. Successful login clears only its own IP backoff.

Render: the deployment assumption is that load-balancer requests arrive from internal `10.0.0.0/8` addresses. The Blueprint leaves `SOE_TRUSTED_PROXIES` as `sync: false`: verify socket peers and the forwarded chain for your service, then supply this range or narrower confirmed ingress CIDRs before deployment. Do not allow arbitrary private-network clients to bypass ingress.

Fly: [Fly documents](https://fly.io/docs/networking/services/) that it sets `Fly-Client-IP`. The expected edge peers use private 6PN addresses, but exact trusted ingress ranges must be verified for the app. The backend currently reads X-Forwarded-For, not Fly-Client-IP. The template deliberately leaves `SOE_TRUSTED_PROXIES` empty pending verification: set verified proxy CIDRs and confirm the right-to-left chain resolves the real client, or place a controlled proxy that overwrites X-Forwarded-For from the provider header. Never trust a provider header from arbitrary direct clients or assume every machine in the private 6PN is trusted ingress. Restrict direct API access.
