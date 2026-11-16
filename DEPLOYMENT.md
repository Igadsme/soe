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

The current worker advances queued workflows to rights review; it has no atomic claim/locking protocol. Keep one worker for now; multiple replicas can select the same workflow. Add transactional job claiming before relying on parallel processing. Redis is internal and reserved for future queue integration; the current worker polls PostgreSQL.

Only API and frontend ports are published, on `127.0.0.1` by default. Terminate HTTPS with a reverse proxy and route to these ports. Named volumes persist PostgreSQL and shared media; configure encrypted backups and test restores. Rebuild the frontend whenever its public API URL changes. Use `.env` and secret-management tooling appropriate for your host; never bake credentials into images.

The existing `docker-compose.yml` remains the development configuration. Production infrastructure does not complete the provider OAuth, publishing, analytics, or durable job-processing features; those still need authorized accounts and further implementation.
