# Soe

Soe is an authorization-first content intelligence and clipping platform. This repository contains the M0 foundation and a functional local vertical slice for authorized source registration, rights validation, deterministic highlight ranking, clip records, and operational visibility.

## Quick start

```bash
cp .env.example .env
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
uvicorn app.main:app --app-dir backend --reload
```

Open http://localhost:8000/docs for the API or http://localhost:3000 after starting the frontend:

```bash
npm install --prefix frontend
npm run dev --prefix frontend
```

The default development database is SQLite. `docker compose up --build` starts PostgreSQL, Redis, the API, and the frontend.

This build deliberately does not claim live platform publishing, revenue, or rights for public URLs. Production integrations require credentials, account approval, and explicit rights records. A real local E2E pipeline is available with `PYTHONPATH=backend .venv2/bin/python scripts/run_e2e.py`; it generates original test media and writes captioned MP4s under `outputs/e2e/`.

## Current status

M0 is implemented, plus the core MVP domain contracts: source registry, rights gate, deterministic ranking, pipeline execution records, and dashboard overview. See [ROADMAP.md](ROADMAP.md) and [CHANGELOG.md](CHANGELOG.md).
