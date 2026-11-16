# Production readiness

This release hardens the current control-plane slice. It is deployable as a staging service, but it is not yet the full autonomous clipping product.

## Included

- SQLAlchemy async runtime dependencies, connection pre-ping, and Alembic migrations.
- Migration-before-start API container entrypoint.
- PostgreSQL readiness checks and Compose health dependencies.
- Configurable CORS and API-key protection for mutations.
- Production startup guard requiring `SOE_API_KEY`.
- Liveness (`/health`) and database readiness (`/ready`) endpoints.
- Foreign keys, duplicate-source handling, rights datetime normalization, and clip interval validation.
- CI for backend tests/migrations and frontend audit/build.
- Next.js upgrade with zero high-severity production npm audit findings at review time.

## Required before a public launch

- Replace the baseline API key with user identity, roles, OAuth, token rotation, and secret-manager integration.
- Implement and contract-test YouTube/Twitch adapters using authorized accounts.
- Add object storage, durable queue workers, media ingestion, transcription, scene analysis, rendering, review, publishing, and analytics.
- Add workflow idempotency, retry/dead-letter handling, rate limits, audit log shipping, metrics, tracing, error tracking, backups, restore drills, and disaster recovery.
- Complete legal/rights review, retention/deletion workflows, provider quotas, abuse controls, load testing, and penetration testing.

Do not enable demo mode or publish real media until those provider and operational controls are complete.
