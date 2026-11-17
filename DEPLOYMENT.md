# Deployment

## Production build and run

Copy `.env.example` to `.env` and replace every credential placeholder. Set the public HTTPS API URL (`NEXT_PUBLIC_API_URL`) and exact frontend CORS origin before building; the frontend URL is baked into the JavaScript. Use `SOE_ENV=production`, `SOE_DEMO_MODE=false`, a random session secret of at least 32 characters, and PostgreSQL. Keep `.env` out of version control. Use `postgresql+psycopg://` for `SOE_DATABASE_URL`, matching the `POSTGRES_*` values, with URL-encoded credentials.

Use an admin password hash in production (plaintext password login is development-only). Generate it interactively after building, using a hidden password prompt:

```sh
docker compose -f docker-compose.prod.yml build api frontend
docker compose -f docker-compose.prod.yml run --rm --no-deps -e SOE_RUN_MIGRATIONS=false api python -c 'from getpass import getpass; from app.auth import make_password_hash; print(make_password_hash(getpass()))'
```

Store the resulting hash as a **single-quoted** `SOE_ADMIN_PASSWORD_HASH` in `.env` so Compose preserves its `$` separator. The build uses placeholders safely; they must be replaced before starting services.

Validate configuration, migrate once, then start:

```sh
docker compose -f docker-compose.prod.yml config --quiet
docker compose -f docker-compose.prod.yml up -d   # one-shot `migrate` service runs `alembic upgrade head` first
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/ready
curl --fail http://127.0.0.1:3000/
docker compose -f docker-compose.prod.yml ps
```

The entrypoint validates production configuration, optionally migrates, and runs Gunicorn with Uvicorn workers. Custom commands run after the optional migration. `SOE_RUN_MIGRATIONS` defaults to true in the image; production Compose and the example environment set it to false for normal starts. The one-shot `migrate` service runs `alembic upgrade head` once per `up`; `api` and `worker` start only after it exits successfully, so replicas never race on migrations. `/health` is liveness; `/ready` checks database connectivity. The worker healthcheck requires a recent successful database polling cycle.

Set `SOE_WEB_CONCURRENCY` to change Gunicorn processes per API container and recreate the API. To change background worker replicas:

```sh
docker compose -f docker-compose.prod.yml up -d --scale worker=2
```

Workers can scale safely against the same PostgreSQL database and shared media volume. Processing jobs use transactional `FOR UPDATE SKIP LOCKED` claims (conditional UPDATE on SQLite), 120-second renewable leases, and ownership-fenced completion. Crashed claims are reclaimed after expiry; each claim consumes an attempt. Jobs retry with exponential backoff (2, 4 seconds), then fail after three attempts. SIGTERM stops claiming and lets the current job finish while renewing its lease. Redis supplies rate limits; job persistence is in PostgreSQL.

Only API and frontend ports are published, on `127.0.0.1` by default. Terminate HTTPS with a reverse proxy and route to these ports. Named volumes persist PostgreSQL and shared media; configure encrypted backups and test restores. Rebuild the frontend whenever its public API URL changes. Use `.env` and secret-management tooling appropriate for your host; never bake credentials into images.

The existing `docker-compose.yml` remains the development configuration. Production infrastructure does not complete the provider OAuth, publishing, analytics, features; those still need authorized accounts and further implementation.

Production requires `SOE_REDIS_URL` (Compose defaults API/worker to `redis://redis:6379/0`). All API workers/replicas must share this Redis database. General and login counters use 60-second fixed windows starting with the first request, with the existing limits. If Redis fails, general requests fail open with a sanitized warning; login fails closed with HTTP 503, `login temporarily unavailable`, and `Retry-After: 60`. Empty URLs use the in-memory sliding windows only in development/tests; production validation rejects them. Redis data loss clears counters.

Run `PYTHONPATH=backend python -m app.worker` locally after `alembic upgrade head`.
The worker validates production settings at startup, touches `/tmp/soe-worker-heartbeat`
while polling and during long jobs, and cleans up expired/revoked sessions hourly.
`SOE_SESSION_RETENTION_SECONDS` defaults to 604800 (seven days after expiry or
revocation). Set `SOE_ENV_FILE` to choose the settings dotenv file; tests use a
nonexistent path and disposable databases. `/ready` resolves migrations relative
to the installed package, independently of the launch directory.

Monitor queued age, attempts, failed jobs and disk usage. Failed jobs remain
available for inspection through the authenticated jobs API; submitting processing
again without the original idempotency key creates a new job. Keys are hashed in
the DB and replay completed responses. Shared media storage must be mounted at the
same path in every API and worker replica. Each attempt writes its own workspace;
plan retention for abandoned attempt files. Publication queue execution is still
incomplete and separate from source processing.
