# Deployment

The supplied Compose file is a development/staging baseline. Production requires HTTPS termination, managed PostgreSQL, encrypted backups with restore drills, a secret manager, object storage, worker supervision, restricted network access, exact CORS origins, API authentication, and platform OAuth callback URLs.

The API image runs `alembic upgrade head` before Uvicorn. Deploy `/health` as a liveness probe and `/ready` as a readiness probe. Do not enable `SOE_DEMO_MODE` in production. Configure a strong `SOE_API_KEY`; this is a baseline service credential, not a replacement for user identity and role-based authorization.

Provider OAuth, media ingestion, transcription, rendering, publishing, analytics, and durable background workers still require implementation and authorized provider accounts. They are release blockers for the full product, even though the service foundation can now be deployed safely as a staging control plane.
