#!/usr/bin/env bash
# Starts the real FastAPI backend + worker on a throwaway SQLite database for Playwright.
# Credentials here are local test fixtures only (development env, loopback), never deployment secrets.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${SOE_E2E_PYTHON:-python}"
WORK="${SOE_E2E_DIR:-$(mktemp -d /tmp/soe-e2e.XXXXXX)}"
mkdir -p "$WORK/media"
export SOE_ENV=development SOE_DEMO_MODE=false SOE_DATABASE_URL="sqlite:///$WORK/e2e.db" SOE_MEDIA_ROOT="$WORK/media"
export SOE_ADMIN_USERNAME=operator SOE_ADMIN_PASSWORD="${SOE_E2E_PASSWORD:-e2e-local-password}"
export SOE_SESSION_SECRET="e2e-local-session-secret-0123456789abcdef" SOE_CORS_ORIGINS=http://localhost:3000
export SOE_RATE_LIMIT_PER_MINUTE="${SOE_E2E_RATE_LIMIT:-600}" SOE_PUBLISHING_ENABLED=false PYTHONPATH="$ROOT/backend" SOE_WHISPER_CACHE_DIR="${SOE_E2E_WHISPER_CACHE:-$HOME/.cache/soe-whisper}"
cd "$ROOT"
"$PY" -m alembic upgrade head >"$WORK/alembic.log" 2>&1
"$PY" -m app.worker >"$WORK/worker.log" 2>&1 &
WORKER=$!
trap 'kill $WORKER 2>/dev/null || true' EXIT
echo "e2e workdir: $WORK"
exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level warning
