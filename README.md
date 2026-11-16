# Soe

Soe is an authorization-first content intelligence and clipping platform. This repository contains the M0 foundation and a functional local vertical slice for authorized source registration, rights validation, deterministic highlight ranking, clip records, and operational visibility.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
PYTHONPATH=backend alembic upgrade head
uvicorn app.main:app --app-dir backend --reload
```

With no `.env`, development defaults use SQLite and a demo-only authentication bypass. If a production `.env` exists, use a separate shell/worktree or explicitly override production settings before running locally. API docs are disabled; liveness is at http://localhost:8000/health. Open http://localhost:3000 after starting the frontend:

```bash
npm ci --prefix frontend
npm run dev --prefix frontend
```

The default development database is SQLite. `docker compose up --build` starts PostgreSQL, Redis, the API, and the frontend.

This build deliberately does not claim live platform publishing, revenue, or rights for public URLs. Production integrations require credentials, account approval, and explicit rights records. A real local E2E pipeline is available with `PYTHONPATH=backend .venv2/bin/python scripts/run_e2e.py`; it generates original test media and writes captioned MP4s under `outputs/e2e/`.

## Current status

M0 is implemented, plus the core MVP domain contracts: source registry, rights gate, deterministic ranking, pipeline execution records, and dashboard overview. See [ROADMAP.md](ROADMAP.md) and [CHANGELOG.md](CHANGELOG.md).

For production, copy `.env.example` to `.env`, replace placeholders using [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md), and use `docker compose -f docker-compose.prod.yml up --build -d`. The product is **not ready for public launch** until the documented owner actions are complete. See [BACKUP_RESTORE.md](BACKUP_RESTORE.md) and run `scripts/ci_checks.sh` for local quality checks. `src/soe` is a separate unused scaffold retained for an owner decision; the production API is `backend/app`.
